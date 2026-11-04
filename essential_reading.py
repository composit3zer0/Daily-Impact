"""Today's Essential Reading: seven daily reading recommendations for Daily Impact.

Division of labour:
  * Script (this file): collect candidates from curated RSS feeds, open every
    article, confirm the publication date (<= 14 days old), reject paywalls and
    blocked domains, compute reading time, enforce no-repeat, check summary
    length, render the HTML section, and record what was published.
  * Gemini: choose the best verified candidate per category and write the
    summaries (reading_prompt.md), and, as a fallback only, search the web for
    a category that has no usable feed candidates. Fallback URLs go through
    the same checks.

Nothing in here may stop the main Daily Impact from publishing: build_section()
always returns HTML, using omission notes where a category could not be filled.
"""

import html
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from feedparser.datetimes import _parse_date

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(HERE, 'reading_sources.json')
PROMPT_FILE = os.path.join(HERE, 'reading_prompt.md')
HISTORY_FILE = os.path.join(HERE, 'reading_history.json')

CATEGORIES = [
    (1, "Health and Wellness"),
    (2, "Religion and Spirituality"),
    (3, "Psychology, Psychiatry, and Mental Health"),
    (4, "Social Media and Sociocultural Trends"),
    (5, "Alternative Medicine and Herbology"),
    (6, "Sex and Relationships"),
    (7, "Parenting and Special Needs"),
]
CATEGORY_NAMES = dict(CATEGORIES)

MAX_AGE_DAYS = 14
WORDS_PER_MINUTE = 230
MIN_ARTICLE_WORDS = 350          # shorter than this is usually a teaser or a blocked page
SUMMARY_WORDS = (80, 120)
MAX_CANDIDATES_PER_CATEGORY = 12
TITLE_SIMILARITY = 0.85          # headline match against past Daily Impact stories

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": USER_AGENT,
                     "Accept-Language": "en-US,en;q=0.9"})

PAYWALL_MARKERS = [
    "subscribe to continue reading", "subscribe to keep reading",
    "to continue reading, subscribe", "this article is for subscribers",
    "subscribers only", "for subscribers only", "members-only content",
    "become a member to read", "sign in to continue reading",
    "you have reached your limit of free articles",
    "you've reached your free article limit", "create a free account to continue reading",
    "log in to read the full article", "purchase this article",
]


# --------------------------------------------------------------------------- utils

def log(msg):
    print(f"[reading] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def normalize_url(url):
    """Canonical form for no-repeat checks: no tracking params, fragment, www or trailing slash."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    query = [(k, v) for k, v in parse_qsl(parts.query)
             if not k.lower().startswith(('utm_', 'fbclid', 'gclid', 'mc_', 'ref'))]
    path = parts.path.rstrip('/') or '/'
    return urlunsplit(('https', host, path, urlencode(query), ''))


def domain_of(url):
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith('www.') else host


def domain_matches(host, domain):
    return host == domain or host.endswith('.' + domain)


def normalize_title(title):
    return re.sub(r'[^a-z0-9 ]+', '', html.unescape(title or '').lower()).strip()


def parse_date(value):
    if not value:
        return None
    t = _parse_date(str(value).strip())
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc)


def word_count(text):
    return len(re.findall(r"\b[\w'’-]+\b", text or ''))


# ------------------------------------------------------------------ article checks

def _jsonld_objects(soup):
    for tag in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(tag.string or '')
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                yield obj
                if '@graph' in obj:
                    stack.append(obj['@graph'])


def extract_published(soup):
    """Publication date from structured page metadata only. Returns (datetime, where_found)."""
    for obj in _jsonld_objects(soup):
        if obj.get('datePublished'):
            d = parse_date(obj['datePublished'])
            if d:
                return d, 'json-ld'
    for attrs in ({'property': 'article:published_time'}, {'name': 'article:published_time'},
                  {'name': 'citation_publication_date'}, {'name': 'citation_date'},
                  {'name': 'dc.date'}, {'name': 'DC.date.issued'}, {'name': 'parsely-pub-date'},
                  {'name': 'sailthru.date'}, {'itemprop': 'datePublished'},
                  {'name': 'pubdate'}, {'name': 'publish-date'}):
        tag = soup.find('meta', attrs=attrs)
        if tag and tag.get('content'):
            d = parse_date(tag['content'])
            if d:
                return d, 'meta:' + list(attrs.values())[0]
    tag = soup.find(attrs={'itemprop': 'datePublished'})
    if tag and (tag.get('datetime') or tag.get('content')):
        d = parse_date(tag.get('datetime') or tag.get('content'))
        if d:
            return d, 'itemprop'
    tag = soup.find('time', attrs={'datetime': True})
    if tag:
        d = parse_date(tag['datetime'])
        if d:
            return d, 'time-tag'
    return None, None


def detect_paywall(soup, text):
    for obj in _jsonld_objects(soup):
        free = obj.get('isAccessibleForFree')
        if free is not None and str(free).strip().lower() in ('false', '0', 'no'):
            return 'json-ld isAccessibleForFree=false'
    tier = soup.find('meta', attrs={'property': 'article:content_tier'})
    if tier and (tier.get('content') or '').lower() in ('locked', 'metered'):
        return f"content_tier={tier['content']}"
    low = text.lower()
    for marker in PAYWALL_MARKERS:
        if marker in low:
            return f'paywall text: "{marker}"'
    return None


def article_text(soup):
    """Readable body text: the <article>/<main>/<body> block with the most paragraph text."""
    for tag in soup(['script', 'style', 'noscript', 'nav', 'header', 'footer',
                     'aside', 'form', 'figure', 'svg', 'iframe']):
        tag.decompose()
    best = ''
    for root in soup.find_all(['article', 'main']) + [soup.body or soup]:
        paras = [p.get_text(' ', strip=True) for p in root.find_all('p')]
        text = '\n'.join(p for p in paras if p)
        if word_count(text) > word_count(best):
            best = text
    return best


def verify_article(url, feed_date=None, blocked_domains=(), now=None):
    """Open an article and check it against the selection rules.

    Returns a dict with ok, reason, final_url, title, published, date_source,
    words, minutes and an excerpt for Gemini to summarise from.
    """
    now = now or datetime.now(timezone.utc)
    result = {'url': url, 'ok': False}
    host = domain_of(url)
    if any(domain_matches(host, d) for d in blocked_domains):
        result['reason'] = f'blocked domain {host}'
        return result
    try:
        resp = HTTP.get(url, timeout=25, allow_redirects=True)
    except requests.RequestException as e:
        result['reason'] = f'fetch failed: {type(e).__name__}'
        return result
    result['final_url'] = resp.url
    result['status'] = resp.status_code
    if resp.status_code != 200:
        result['reason'] = f'HTTP {resp.status_code}'
        return result
    final_host = domain_of(resp.url)
    if any(domain_matches(final_host, d) for d in blocked_domains):
        result['reason'] = f'redirected to blocked domain {final_host}'
        return result

    soup = BeautifulSoup(resp.text, 'html.parser')
    og = soup.find('meta', attrs={'property': 'og:title'})
    result['title'] = html.unescape((og.get('content') if og else None)
                                    or (soup.title.string if soup.title else '') or '').strip()
    site = soup.find('meta', attrs={'property': 'og:site_name'})
    result['site_name'] = html.unescape(site['content']).strip() if site and site.get('content') else None

    published, source = extract_published(soup)
    if not published and feed_date:
        published, source = feed_date, 'publisher feed'
    result['published'] = published.isoformat() if published else None
    result['date_source'] = source

    paywall = detect_paywall(soup, resp.text)
    text = article_text(soup)
    words = word_count(text)
    result['words'] = words
    result['minutes'] = max(1, math.ceil(words / WORDS_PER_MINUTE))
    result['excerpt'] = text[:6000]

    if not published:
        result['reason'] = 'no publication date in page metadata or feed'
    elif published > now + timedelta(days=1):
        result['reason'] = f'publication date in the future ({published.date()})'
    elif now - published > timedelta(days=MAX_AGE_DAYS):
        result['reason'] = f'too old ({published.date()})'
    elif paywall:
        result['reason'] = paywall
    elif words < MIN_ARTICLE_WORDS:
        result['reason'] = f'only {words} words of readable text (teaser or blocked page)'
    else:
        result['ok'] = True
        result['reason'] = 'ok'
    return result


# ----------------------------------------------------------------------- feeds

def fetch_feed(url, now=None):
    """Return (entries, error). Each entry: title, url, published (datetime or None), summary."""
    now = now or datetime.now(timezone.utc)
    try:
        resp = HTTP.get(url, timeout=25)
    except requests.RequestException as e:
        return [], f'fetch failed: {type(e).__name__}'
    if resp.status_code != 200:
        return [], f'HTTP {resp.status_code}'
    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        return [], 'no entries (not a feed?)'
    entries = []
    for e in parsed.entries:
        t = e.get('published_parsed') or e.get('updated_parsed')
        published = datetime(*t[:6], tzinfo=timezone.utc) if t else None
        link = e.get('link')
        if not link:
            continue
        summary = BeautifulSoup(e.get('summary', ''), 'html.parser').get_text(' ', strip=True)
        entries.append({'title': html.unescape(e.get('title', '')).strip(), 'url': link,
                        'published': published, 'summary': summary[:400]})
    return entries, None
