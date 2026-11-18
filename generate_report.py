"""Daily Impact: generate today's briefing with Claude and publish it.

Claude (via headless Claude Code, see claude_llm.py) researches today's news with
web search and writes the full HTML briefing following SYSTEM_PROMPT (a repo
secret). essential_reading.py then adds "Today's Essential Reading".

A briefing that comes back incomplete (cut off, or missing its closing sections)
is retried once and otherwise NOT published: the run fails and the watchdog
raises an alert instead of a broken page going live.

News freshness (owner's rule, 2026-11-18): every News story carries its source's
publication date. Market stories (prices of stocks, crypto, commodities, currencies,
bonds) must be at most 24 hours old and every other story at most 48 hours old.
A briefing with stale or undated stories is retried once; whatever is still stale
is then removed from the page rather than published.
"""
import glob
import os
import re
import sys
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

import claude_llm

# Sections every edition must contain, in order (from the SYSTEM_PROMPT template).
REQUIRED_SECTIONS = ["News", "Editorial", "Dhamma Corner", "Personal Reflection"]

BANGKOK = ZoneInfo("Asia/Bangkok")
MARKET_MAX_HOURS = 24            # market news: prices of stocks, crypto, commodities, currencies, bonds
NEWS_MAX_HOURS = 48              # every other News story
FUTURE_TOLERANCE = timedelta(hours=3)
STORY_DATE_CSS = ('<style>.story-date { font-size: 0.78rem; color: #7ecfd4; letter-spacing: 0.05em; '
                  'margin-bottom: 8px; }</style>')


def extract_html(raw):
    """Strip anything before the HTML document."""
    for marker in ('<!DOCTYPE html>', '<!doctype html>', '<html'):
        if marker in raw:
            return raw[raw.index(marker):]
    return raw


def completeness_problems(report):
    """Reasons the briefing looks incomplete (empty list = complete)."""
    problems = []
    if len(report) < 5000:
        problems.append(f'only {len(report)} characters')
    if not re.search(r'</html>\s*$', report, re.I):
        problems.append('does not end with </html> (output cut off?)')
    headers = [h.strip().lower() for h in re.findall(r'class="section-header"[^>]*>([^<]+)<', report)]
    for name in REQUIRED_SECTIONS:
        if name.lower() not in headers:
            problems.append(f'missing the "{name}" section')
    return problems


# ------------------------------------------------------------ news freshness

def _section_bounds(report, name):
    """(start, end) of a section's content: from its header to the next section header."""
    m = re.search(r'class="section-header"[^>]*>\s*' + re.escape(name) + r'\s*<', report, re.I)
    if not m:
        return None
    nxt = re.search(r'class="section-header"', report[m.end():])
    return m.end(), (m.end() + nxt.start() if nxt else len(report))


def _div_end(report, start):
    """Index just past the </div> that closes the <div ...> opening at start."""
    depth = 0
    for m in re.finditer(r'<div\b|</div\s*>', report[start:], re.I):
        depth += 1 if m.group(0).lower().startswith('<div') else -1
        if depth == 0:
            return start + m.end()
    return len(report)


def _attr(tag, name):
    m = re.search(name + r'\s*=\s*"([^"]*)"', tag, re.I)
    return m.group(1).strip() if m else None


def news_stories(report):
    """Each News story as dict(start, end, title, kind, published) (published may be None)."""
    bounds = _section_bounds(report, 'News')
    if not bounds:
        return []
    stories = []
    pos, end = bounds
    while True:
        m = re.compile(r'<div[^>]*class="story"[^>]*>', re.I).search(report, pos, end)
        if not m:
            break
        stop = _div_end(report, m.start())
        block = report[m.start():stop]
        title = re.search(r'class="story-title"[^>]*>(.*?)</div>', block, re.I | re.S)
        title = re.sub(r'<[^>]+>|\s+', ' ', title.group(1)).strip() if title else '(untitled)'
        tag = re.search(r'<[^>]*class="story-date"[^>]*>', block, re.I)
        published, kind = None, None
        if tag:
            kind = (_attr(tag.group(0), 'data-kind') or '').lower() or None
            raw = _attr(tag.group(0), 'data-published')
            try:
                published = datetime.fromisoformat(raw.replace('Z', '+00:00')) if raw else None
                if published and published.tzinfo is None:
                    published = published.replace(tzinfo=BANGKOK)
            except ValueError:
                published = None
        stories.append({'start': m.start(), 'end': stop, 'title': title,
                        'kind': kind, 'published': published})
        pos = stop
    return stories


def stale_news(report, now):
    """News stories that break the freshness rules, as (story, reason) pairs."""
    out = []
    for st in news_stories(report):
        # A story not clearly labelled "general" gets the stricter market limit.
        limit = NEWS_MAX_HOURS if st['kind'] == 'general' else MARKET_MAX_HOURS
        if not st['published']:
            out.append((st, 'no publication date (story-date with data-published)'))
        elif st['published'] > now + FUTURE_TOLERANCE:
            out.append((st, f"dated in the future ({st['published'].isoformat()})"))
        elif now - st['published'] > timedelta(hours=limit):
            hours = (now - st['published']).total_seconds() / 3600
            out.append((st, f"{hours:.0f} hours old (limit {limit} h for {st['kind'] or 'market'} news)"))
    return out


def remove_stale_news(report, now):
    """Drop stories that break the freshness rules; never leaves the News section blank."""
    stale = stale_news(report, now)
    for st, reason in sorted(stale, key=lambda x: -x[0]['start']):
        print(f"News story removed ({reason}): {st['title']}")
        report = report[:st['start']] + report[st['end']:]
    if stale:
        bounds = _section_bounds(report, 'News')
        if bounds:
            start, end = bounds
            body = report[start:end]
            # Subsection labels left with no story under them.
            body = re.sub(r'<div[^>]*class="subsection-label"[^>]*>[^<]*</div>\s*'
                          r'(?=<div[^>]*class="subsection-label"|</div>)', '', body)
            if not news_stories(report[:start] + body + report[end:]):
                scroll = re.search(r'<div[^>]*class="news-scroll"[^>]*>', body)
                note = ('<div class="story"><div class="story-summary">No news story met today\'s '
                        'freshness rules (market news within 24 hours, other news within 48 hours).'
                        '</div></div>')
                at = scroll.end() if scroll else 0
                body = body[:at] + note + body[at:]
            report = report[:start] + body + report[end:]
    return report


def add_story_date_style(report):
    if '.story-date' in report or '</head>' not in report:
        return report
    return report.replace('</head>', STORY_DATE_CSS + '\n</head>', 1)


def freshness_rules(now):
    return (
        f"NEWS FRESHNESS RULES (these override anything else about which stories to include). "
        f"Market news (prices or moves of stocks, indices, crypto, commodities such as oil or gold, "
        f"currencies or bonds) must come from a source article published within the last "
        f"{MARKET_MAX_HOURS} hours. Every other News story must come from a source article published "
        f"within the last {NEWS_MAX_HOURS} hours. Open each source article and take its publication "
        f"date and time from the article itself. Drop any story whose date you cannot confirm or that "
        f"is too old, even a strong one: fewer stories is better than old ones. "
        f"Right after each story's title, add exactly one line: "
        f'<div class="story-date" data-published="YYYY-MM-DDTHH:MM+07:00" data-kind="market">'
        f"Published 17 Nov 2026, 14:00 Bangkok</div> "
        f'with the real publication time converted to Bangkok time, and data-kind="market" for market '
        f'news or data-kind="general" for everything else. Stories without this line, or older than '
        f"the limits, are removed automatically. The time now is {now.strftime('%Y-%m-%d %H:%M')} Bangkok."
    )


def briefing_prompt(now, feedback=''):
    return (
        f"Generate today's daily report. "
        f"Today is {now.strftime('%A')}, {now.strftime('%d %B %Y')}, 8:00 AM Bangkok time "
        f"(Asia/Bangkok, UTC+7). "
        f"Use web search to find today's actual current news before writing, "
        f"and open articles to check facts and publication dates. "
        f"{freshness_rules(now)} "
        f"The edition must contain all of its sections in order: {', '.join(REQUIRED_SECTIONS)}. "
        f"Keep each section to its usual length so the whole page fits comfortably in one answer. "
        f"Your final answer must be only the complete, valid HTML document, starting with "
        f"<!DOCTYPE html> and ending with </html>: no preamble, no notes, no markdown, no code fences."
        + (f"\n\nIMPORTANT: {feedback}" if feedback else '')
    )


def finish_briefing(report, now):
    """Enforce the News freshness rules on a complete briefing."""
    return add_story_date_style(remove_stale_news(report, now))


def generate_briefing(now, system_prompt):
    feedback = ''
    best = None                         # complete briefing with the fewest stale stories so far
    for attempt in (1, 2):
        raw = claude_llm.ask(briefing_prompt(now, feedback), system=system_prompt,
                             tools=claude_llm.WEB_TOOLS, timeout=1500, attempts=2,
                             label=f'briefing #{attempt}')
        report = extract_html(raw.strip())
        problems = completeness_problems(report)
        print(f"Report extracted: {len(report)} characters")
        if problems:
            print(f"Briefing attempt {attempt} incomplete: {'; '.join(problems)}")
            feedback = ("Your previous answer was rejected because it " + '; '.join(problems) +
                        ". Write the complete page again, keeping every section concise.")
            continue
        stale = stale_news(report, now)
        if not stale:
            return finish_briefing(report, now)
        print(f"Briefing attempt {attempt}: {len(stale)} News stories break the freshness rules")
        if best is None or len(stale) < best[1]:
            best = (report, len(stale))
        feedback = ("Your previous answer had News stories that break the freshness rules: " +
                    '; '.join(f'"{st["title"]}" ({reason})' for st, reason in stale) +
                    ". Replace or drop them and write the complete page again.")
    if best:
        return finish_briefing(best[0], now)
    raise SystemExit("ERROR: briefing still incomplete after a retry; not publishing a broken page")


def main():
    now = datetime.now(BANGKOK)
    archive_filename = now.strftime('%Y-%m-%d') + '.html'
    print(f"Generating Daily Impact for {now.strftime('%d %B %Y')} with Claude ({claude_llm.DEFAULT_MODEL})...")
    system_prompt = os.environ['SYSTEM_PROMPT']

    report = generate_briefing(now, system_prompt)

    # Add "Today's Essential Reading". This step never blocks the main report:
    # on any failure the briefing is published without the section.
    try:
        from essential_reading import add_essential_reading
        report = add_essential_reading(report, now)
    except Exception as e:
        print(f"WARNING: Essential Reading skipped: {e}")

    # Write today's report as index.html (live page)
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(report)
    print("Written: index.html")

    # Write archive copy
    with open(archive_filename, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"Written: {archive_filename}")

    # Rebuild archive index
    archive_files = sorted(glob.glob('20*.html'), reverse=True)
    archive_links = '\n'.join([
        f'<li><a href="{f}" target="_blank" rel="noopener noreferrer">'
        f'{f.replace(".html", "")}</a></li>'
        for f in archive_files
    ])

    archive_html = f"""<!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>The Daily Impact — Archive</title>
      <style>
        body {{
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0a0e16;
          color: #edeff2;
          max-width: 640px;
          margin: 60px auto;
          padding: 0 24px;
        }}
        h1 {{ color: #c9a84c; letter-spacing: 0.05em; }}
        p {{ color: #8a9bb0; }}
        ul {{ list-style: none; padding: 0; }}
        li {{ margin: 12px 0; }}
        a {{
          color: #7ecfd4;
          text-decoration: none;
          font-size: 1.05rem;
        }}
        a:hover {{ color: #c9a84c; }}
      </style>
    </head>
    <body>
      <h1>The Daily Impact</h1>
      <p>Daily intelligence briefing — archive of all issues.</p>
      <ul>{archive_links}</ul>
    </body>
    </html>"""

    with open('archive.html', 'w', encoding='utf-8') as f:
        f.write(archive_html)
    print("Written: archive.html")

    print("Done.")


if __name__ == "__main__":
    main()
