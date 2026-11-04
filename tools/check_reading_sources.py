"""Check every feed in reading_sources.json from the machine it runs on.

Run on a GitHub runner (the daily job's network) when changing the source list:
    pip install feedparser requests beautifulsoup4 && python tools/check_reading_sources.py

For each feed: HTTP status, entry count, entries from the last 14 days.
For up to 3 recent entries per feed: run the same article checks the daily
job uses (loads, dated <= 14 days, no paywall, enough text).
Writes a Markdown table to $GITHUB_STEP_SUMMARY when available.
"""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import essential_reading as er

now = datetime.now(timezone.utc)
cfg = er.load_json(er.SOURCES_FILE, {})
blocked = cfg.get('blocked_domains', [])

def check_feed(src, url):
    entries, err = er.fetch_feed(url)
    if err:
        return {'source': src['name'], 'feed': url, 'error': err, 'articles': []}
    recent = [e for e in entries if e['published'] and now - e['published'] <= timedelta(days=14)]
    arts = [er.verify_article(e['url'], e['published'], blocked) for e in recent[:3]]
    return {'source': src['name'], 'feed': url, 'error': None, 'entries': len(entries),
            'recent': len(recent), 'articles': arts}

jobs = [(s, f) for s in cfg['sources'] for f, _cats in er.feeds_of(s)]
with ThreadPoolExecutor(8) as ex:
    results = list(ex.map(lambda j: check_feed(*j), jobs))

lines = ["| Source | Feed | Feed result | Last 14 d | Articles passing (of tested) | Rejection reasons / date source |",
         "|---|---|---|---|---|---|"]
for r in results:
    if r['error']:
        lines.append(f"| {r['source']} | {r['feed']} | ❌ {r['error']} | | | |")
        continue
    ok = sum(a['ok'] for a in r['articles'])
    notes = '; '.join(sorted({(a['date_source'] or '-') if a['ok'] else a['reason'] for a in r['articles']}))
    lines.append(f"| {r['source']} | {r['feed']} | ✅ {r['entries']} items | {r['recent']} | "
                 f"{ok}/{len(r['articles'])} | {notes} |")
report = '\n'.join(lines)
detail = json.dumps([{k: v for k, v in a.items() if k != 'excerpt'}
                     for r in results for a in r['articles']], indent=1, default=str)
print(detail)
print('=== SUMMARY TABLE ===')
print(report)
if os.environ.get('GITHUB_STEP_SUMMARY'):
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
        f.write('## Reading source check\n\n' + report + '\n')
