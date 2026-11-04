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
TIME_BUDGET_SECONDS = 8 * 60     # the whole section; remaining fallback work is skipped after this
_deadline = [float('inf')]


def out_of_time():
    return time.monotonic() > _deadline[0]

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


# --------------------------------------------------------------------- history

def load_history():
    return load_json(HISTORY_FILE, [])


def past_titles(history, today):
    """Headlines already featured anywhere in Daily Impact (archived reports + reading history)."""
    titles = set()
    for path in sorted(os.listdir(HERE)):
        if re.fullmatch(r'20\d\d-\d\d-\d\d\.html', path) and path != f'{today}.html':
            with open(os.path.join(HERE, path), encoding='utf-8', errors='ignore') as f:
                for t in re.findall(r'class="story-title"[^>]*>(.*?)</', f.read(), re.S):
                    t = normalize_title(re.sub(r'<[^>]+>', '', t))
                    if t:
                        titles.add(t)
    titles.update(normalize_title(h['title']) for h in history if h.get('title'))
    return titles


def is_repeat(url, title, seen_urls, seen_titles):
    if normalize_url(url) in seen_urls:
        return True
    nt = normalize_title(title)
    if not nt:
        return False
    if nt in seen_titles:
        return True
    return any(SequenceMatcher(None, nt, t).ratio() >= TITLE_SIMILARITY
               for t in seen_titles if abs(len(t) - len(nt)) < 20)


def clean_display_url(url):
    """Drop tracking parameters but keep the URL otherwise exactly as the publisher serves it."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(('utm_', 'fbclid', 'gclid', 'mc_'))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


# ------------------------------------------------------------------ candidates

def feeds_of(src):
    """(url, categories) for each feed; a feed is a URL or {"url": ..., "categories": [...]}."""
    for feed in src['feeds']:
        if isinstance(feed, dict):
            yield feed['url'], feed.get('categories') or src['categories']
        else:
            yield feed, src['categories']


def collect_feed_candidates(config, seen_urls, seen_titles, now):
    """All recent, not-yet-featured feed entries, tagged with the categories of their source."""
    jobs = [(src, feed_url, cats) for src in config['sources'] for feed_url, cats in feeds_of(src)]
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda j: (j[0], j[1], j[2], fetch_feed(j[1], now)), jobs))
    by_url = {}
    for src, feed_url, cats, (entries, err) in results:
        if err:
            log(f"feed skipped: {src['name']} {feed_url}: {err}")
            continue
        for e in entries:
            if e['published'] and now - e['published'] > timedelta(days=MAX_AGE_DAYS):
                continue
            key = normalize_url(e['url'])
            if key in by_url:                       # same article in two feeds: union of categories
                by_url[key]['categories'] = sorted(set(by_url[key]['categories']) | set(cats))
                continue
            if is_repeat(e['url'], e['title'], seen_urls, seen_titles):
                continue
            by_url[key] = dict(e, source=src['name'], categories=list(cats))
    cands = list(by_url.values())
    for i, c in enumerate(cands):
        c['id'] = f"F{i}"
    log(f"{len(cands)} fresh feed candidates from {len(results)} feeds")
    return cands


# --------------------------------------------------------------------- Gemini

def _gemini_json(client, model, system, prompt, tools=None):
    """Call Gemini and parse a JSON object from the reply. Returns (data, response)."""
    from google.genai import types
    cfg = dict(system_instruction=system, temperature=0.4, max_output_tokens=8192)
    if tools:
        cfg['tools'] = tools               # JSON mode can't be combined with search tools
    else:
        cfg['response_mime_type'] = 'application/json'
    last_err = None
    for attempt in range(3):
        if out_of_time():
            raise RuntimeError('reading section time budget used up')
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt, config=types.GenerateContentConfig(**cfg))
        except Exception as e:  # noqa: BLE001 - logged and retried
            last_err = e
            log(f"Gemini call failed ({model}, attempt {attempt + 1}): {e}")
            if ' 400 ' in f' {e} ' or 'INVALID_ARGUMENT' in str(e) or 'NOT_FOUND' in str(e):
                break                       # not worth retrying the same model
            time.sleep(8)
            continue
        text = resp.text or ''
        m = re.search(r'\{.*\}', text, re.S)
        try:
            data = json.loads(m.group(0) if m else text)
            if isinstance(data, dict):
                return data, resp
            last_err = ValueError('reply was JSON but not an object')
        except ValueError as e:
            last_err = e
        log(f"Gemini reply was not usable JSON ({model}, attempt {attempt + 1}); retrying")
        if attempt >= 1:
            break                           # two unusable replies in a row: give up on this call
    raise RuntimeError(f'Gemini call failed: {last_err}')


def _context_block(context, history, now):
    recent = [h for h in history
              if now - datetime.fromisoformat(h['date']).replace(tzinfo=timezone.utc) <= timedelta(days=14)]
    lines = ["TODAY'S NEWS HEADLINES (avoid overlapping topics):"]
    lines += [f"- {t}" for t in context.get('headlines', [])] or ['- (none)']
    lines.append("\nTODAY'S DHAMMA CORNER (category 2 should complement, not duplicate):")
    lines.append(context.get('dhamma', '')[:1500] or '(none)')
    lines.append("\nRECENT PICKS (rotate subtopics; don't repeat these themes):")
    for num, name in CATEGORIES:
        picks = [h['title'] for h in recent if h.get('category') == num][-5:]
        lines.append(f"{num}. {name}: " + ('; '.join(picks) if picks else '(none yet)'))
    return '\n'.join(lines)


def shortlist(client, model, system, candidates, context_text):
    """Gemini ranks up to 4 feed candidates per category using titles and teasers only."""
    blocks = []
    for num, name in CATEGORIES:
        pool = [c for c in candidates if num in c['categories']][:150]
        rows = '\n'.join(f"{c['id']} | {c['source']} | {c['title']} | {c['summary'][:200]}" for c in pool)
        blocks.append(f"## Category {num}: {name}\n{rows or '(no candidates)'}")
    prompt = (
        "Step 1 of 2: shortlist. Below are fresh, unpaywalled articles from our curated sources, "
        "grouped by the categories their publication covers. For each category, choose up to 4 "
        "candidate IDs that best meet the selection standards AND genuinely fit that category, "
        "best first. An ID may be used in only one category. Use [] if nothing fits.\n\n"
        f"{context_text}\n\n" + '\n\n'.join(blocks) +
        '\n\nReply with JSON only: {"1": ["F12", ...], "2": [...], ..., "7": [...]}')
    data, _ = _gemini_json(client, model, system, prompt)
    return {num: [i for i in data.get(str(num), []) if isinstance(i, str)][:4] for num, _ in CATEGORIES}


def search_fallback(client, model, system, num, context_text, blocked):
    """Ask Gemini (with Google Search) for candidate URLs for one empty category."""
    from google.genai import types
    prompt = (
        f"Our curated feeds have no suitable article today for category {num}: {CATEGORY_NAMES[num]}. "
        f"Use Google Search to find up to 4 exceptional, freely accessible articles or essays in this "
        f"category published within the last {MAX_AGE_DAYS} days. Exclude these paywalled or unsuitable "
        f"domains: {', '.join(blocked)}. Give the direct article URL exactly as published; do not guess "
        f"URLs. Our script will open and verify each one.\n\n{context_text}\n\n"
        'Reply with JSON only: {"urls": ["https://...", ...]}')
    data, resp = _gemini_json(client, model, system, prompt,
                              tools=[types.Tool(google_search=types.GoogleSearch())])
    urls = [u for u in data.get('urls', []) if isinstance(u, str) and u.startswith('http')]
    try:   # grounding sources are real search results; verify them too
        for chunk in resp.candidates[0].grounding_metadata.grounding_chunks or []:
            if chunk.web and chunk.web.uri:
                urls.append(chunk.web.uri)
    except (AttributeError, IndexError, TypeError):
        pass
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:8]


def write_summaries(client, model, system, verified, context_text, only=None, feedback=''):
    """Gemini picks one verified article per category and writes the summary."""
    blocks = []
    for num, name in CATEGORIES:
        if only and num not in only:
            continue
        rows = []
        for c in verified.get(num, []):
            rows.append(f"### {c['id']} | {c['source']} | {c['title']} | {c['minutes']} min\n"
                        f"{c['excerpt'][:5000]}")
        blocks.append(f"## Category {num}: {name}\n" + ('\n\n'.join(rows) or '(no verified candidates)'))
    prompt = (
        "Step 2 of 2: choose and write. Every candidate below is verified (free, dated within 14 days, "
        "not featured before). For each category choose the single best candidate and write its summary "
        "following the summary format, based only on the article text shown. If no candidate meets the "
        "bar or fits the category, set id to null and give a one-sentence reason.\n"
        f"{feedback}\n{context_text}\n\n" + '\n\n'.join(blocks) +
        '\n\nReply with JSON only: {"1": {"id": "F3", "summary": "..."} or {"id": null, "reason": "..."}, ...}')
    data, _ = _gemini_json(client, model, system, prompt)
    return data


# ---------------------------------------------------------------------- render

SECTION_CSS = """
<style>
  .essential-reading .reading-category { font-size: .78rem; letter-spacing: .12em; text-transform: uppercase; opacity: .75; margin: 0 0 .35rem; }
  .essential-reading .story-title a { color: inherit; text-decoration: none; border-bottom: 1px solid currentColor; }
  .essential-reading .story-title a:hover { opacity: .8; }
  .essential-reading .reading-meta { font-size: .85rem; opacity: .75; margin: .35rem 0 .6rem; }
  .essential-reading .reading-omitted { font-style: italic; opacity: .8; }
</style>"""


def _fmt_date(iso):
    return datetime.fromisoformat(iso).strftime('%d %b %Y').lstrip('0')


def render_section(picks):
    """picks: {num: {...article...}} or {num: {'omitted': reason}} for all seven categories."""
    e = html.escape
    items = []
    for num, name in CATEGORIES:
        p = picks.get(num) or {'omitted': 'No verified article was available today.'}
        head = f'<div class="reading-category">{num}. {e(name)}</div>'
        if 'omitted' in p:
            items.append(f'<div class="story">{head}<div class="story-summary reading-omitted">'
                         f'No recommendation today — {e(p["omitted"])}</div></div>')
            continue
        items.append(
            f'<div class="story">{head}'
            f'<div class="story-title"><a href="{e(p["url"], quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{e(p["title"])}</a></div>'
            f'<div class="reading-meta">Source: {e(p["source"])} · Published: {_fmt_date(p["published"])}'
            f' · Reading time: {p["minutes"]} minutes</div>'
            f'<div class="story-summary"><strong>Summary:</strong> {e(p["summary"])}</div></div>')
    return ('\n    <!-- SECTION: TODAY\'S ESSENTIAL READING (added by essential_reading.py) -->\n'
            f'    {SECTION_CSS}\n'
            '    <div class="card essential-reading">\n'
            "      <div class=\"section-header\">Today's Essential Reading</div>\n      "
            + '\n      '.join(items) + '\n    </div>\n')


def insert_section(report, section_html):
    """Place the section right before Personal Reflection; fall back to the end of the page."""
    patterns = [
        r'<!--\s*SECTION\s*\d*:?\s*PERSONAL REFLECTION',
        r'<div class="card">\s*<div class="section-header">\s*Personal Reflection',
        r'</div>\s*</body>',
        r'</body>',
    ]
    for pat in patterns:
        m = re.search(pat, report, re.I)
        if m:
            return report[:m.start()] + section_html + '\n    ' + report[m.start():]
    return report + section_html


def report_context(report):
    soup = BeautifulSoup(report, 'html.parser')
    headlines = [t.get_text(' ', strip=True) for t in soup.select('.story-title')]
    dhamma = ''
    for header in soup.select('.section-header'):
        if 'dhamma' in header.get_text().lower():
            dhamma = header.parent.get_text(' ', strip=True)
    return {'headlines': headlines, 'dhamma': dhamma}


# ------------------------------------------------------------------- pipeline

def summary_ok(text):
    n = word_count(text or '')
    return SUMMARY_WORDS[0] <= n <= SUMMARY_WORDS[1], n


def build_picks(client, model, report, now, today):
    config = load_json(SOURCES_FILE, {'sources': [], 'blocked_domains': []})
    blocked = config.get('blocked_domains', [])
    with open(PROMPT_FILE, encoding='utf-8') as f:
        system = f.read()
    history = [h for h in load_history() if h['date'] != today]   # a same-day re-run may reuse its picks
    seen_urls = {h['norm_url'] for h in history}
    seen_titles = past_titles(history, today)
    context_text = _context_block(report_context(report), history, now)

    candidates = collect_feed_candidates(config, seen_urls, seen_titles, now)
    by_id = {c['id']: c for c in candidates}
    ranked = shortlist(client, model, system, candidates, context_text) if candidates else {}

    cache = {}

    def verify_many(items):
        todo = [c for c in items if c['url'] not in cache]
        with ThreadPoolExecutor(8) as ex:
            for c, res in zip(todo, ex.map(lambda c: verify_article(c['url'], c.get('published'), blocked, now), todo)):
                cache[c['url']] = res
        return [(c, cache[c['url']]) for c in items]

    def accept(res, title):
        return res['ok'] and not is_repeat(res['final_url'], title or res['title'], seen_urls, seen_titles)

    # An article may be shortlisted in several categories; uniqueness is enforced at the final pick.
    verified = {num: [] for num, _ in CATEGORIES}
    jobs = [(num, by_id[i]) for num, ids in ranked.items() for i in ids if i in by_id]
    for (num, cand), (_, res) in zip(jobs, verify_many([c for _, c in jobs])):
        if not res['ok']:
            log(f"cat {num} rejected {cand['url']}: {res['reason']}")
        elif accept(res, cand['title']) and all(v['id'] != cand['id'] for v in verified[num]):
            verified[num].append(dict(res, id=cand['id'], source=cand['source'],
                                      title=cand['title'] or res['title']))

    # Search fallback for categories the feeds could not fill.
    sid = 0
    for num, _ in CATEGORIES:
        if verified[num]:
            continue
        if out_of_time():
            log(f"cat {num}: time budget used up, skipping search fallback")
            continue
        try:
            urls = search_fallback(client, model, system, num, context_text, blocked)
        except Exception as e:  # noqa: BLE001
            log(f"cat {num} search fallback failed: {e}")
            continue
        for _, res in verify_many([{'url': u} for u in urls]):
            if not res['ok']:
                log(f"cat {num} fallback rejected {res['url']}: {res['reason']}")
                continue
            if not accept(res, res['title']):
                continue
            sid += 1
            verified[num].append(dict(res, id=f"S{sid}",
                                      source=res.get('site_name') or domain_of(res['final_url'])))
    log('verified per category: ' + ', '.join(f"{n}={len(v)}" for n, v in verified.items()))

    # Choose and summarise, then one corrective round for: unknown ids, the same article
    # picked for two categories, and summaries outside 80-120 words.
    choices = write_summaries(client, model, system, verified, context_text)

    def problems():
        bad, taken = {}, {}
        for num, _ in CATEGORIES:
            ch = choices.get(str(num)) or {}
            cid = ch.get('id')
            if not cid:
                continue
            art = next((v for v in verified[num] if v['id'] == cid), None)
            if not art:
                bad[num] = f'id {cid} is not one of the candidates for this category'
                continue
            key = normalize_url(art['final_url'])
            if key in taken:
                bad[num] = f'{cid} was already chosen for category {taken[key]}; choose a different article'
                continue
            taken[key] = num
            ok, n = summary_ok(ch.get('summary'))
            if not ok:
                bad[num] = f'summary was {n} words; it must be 80-120 words'
        return bad, taken

    for _round in range(2):
        bad, taken = problems()
        if not bad:
            break
        log(f"asking Gemini to fix: {bad}")
        fb = ("CORRECTIONS NEEDED (answer only these categories): "
              + '; '.join(f'category {k}: {v}' for k, v in bad.items())
              + ". Each category must use a different article. Articles already used elsewhere today: "
              + (', '.join(next(v['id'] for n in verified for v in verified[n]
                                if normalize_url(v['final_url']) == key) for key in taken) or 'none') + '.')
        try:
            redo = write_summaries(client, model, system, verified, context_text, only=set(bad), feedback=fb)
            for num in bad:
                if str(num) in redo:
                    choices[str(num)] = redo[str(num)]
        except Exception as e:  # noqa: BLE001
            log(f"correction round failed: {e}")

    picks, used = {}, set()
    for num, _ in CATEGORIES:
        ch = choices.get(str(num)) or {}
        art = next((v for v in verified[num] if v['id'] == ch.get('id')), None) if ch.get('id') else None
        if art and normalize_url(art['final_url']) in used:
            art = None
            ch = {'reason': 'the best candidate was already recommended in another category today'}
        if not art:
            reason = None if ch.get('id') else ch.get('reason')
            if not verified[num]:
                reason = 'no freely accessible article from the last 14 days could be verified'
            picks[num] = {'omitted': (reason or 'no candidate met the editorial bar').rstrip('.') + '.'}
            continue
        ok, n = summary_ok(ch.get('summary'))
        if not (60 <= n <= 150):        # far outside the target even after a correction
            picks[num] = {'omitted': 'the summary could not be written to standard today.'}
            continue
        if not ok:
            log(f"cat {num}: publishing summary of {n} words (outside 80-120 after correction)")
        used.add(normalize_url(art['final_url']))
        picks[num] = {
            'url': clean_display_url(art['final_url']), 'title': art['title'], 'source': art['source'],
            'published': art['published'], 'minutes': art['minutes'],
            'summary': re.sub(r'\s+', ' ', ch['summary']).strip(),
        }
    return picks


def add_essential_reading(client, report, now, model=None, fallback_model=None):
    """Return the report with the reading section inserted. Never raises."""
    model = model or os.environ.get('READING_MODEL', '').strip() or 'gemini-flash-latest'
    now_utc = now.astimezone(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    _deadline[0] = time.monotonic() + TIME_BUDGET_SECONDS
    picks = None
    for m in [model] + ([fallback_model] if fallback_model and fallback_model != model else []):
        try:
            log(f"building with model {m}")
            picks = build_picks(client, m, report, now_utc, today)
            break
        except Exception as e:  # noqa: BLE001
            log(f"build failed with {m}: {e}")
            if out_of_time():
                break
    if picks is None:
        picks = {num: {'omitted': 'the reading section could not be compiled today.'}
                 for num, _ in CATEGORIES}

    history = load_history()
    history = [h for h in history if h['date'] != today]   # a same-day re-run replaces that day's picks
    for num, p in picks.items():
        if 'url' in p:
            history.append({'date': today, 'category': num, 'url': p['url'],
                            'norm_url': normalize_url(p['url']), 'title': p['title'],
                            'source': p['source'], 'published': p['published']})
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=1, ensure_ascii=False)
    filled = sum('url' in p for p in picks.values())
    log(f"section published with {filled}/7 recommendations")
    return insert_section(report, render_section(picks))
