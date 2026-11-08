"""Daily Impact: generate today's briefing with Claude and publish it.

Claude (via headless Claude Code, see claude_llm.py) researches today's news with
web search and writes the full HTML briefing following SYSTEM_PROMPT (a repo
secret). essential_reading.py then adds "Today's Essential Reading".

A briefing that comes back incomplete (cut off, or missing its closing sections)
is retried once and otherwise NOT published: the run fails and the watchdog
raises an alert instead of a broken page going live.
"""
import glob
import os
import re
import sys
from datetime import datetime

from zoneinfo import ZoneInfo

import claude_llm

# Sections every edition must contain, in order (from the SYSTEM_PROMPT template).
REQUIRED_SECTIONS = ["News", "Editorial", "Dhamma Corner", "Personal Reflection"]


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


def briefing_prompt(now, feedback=''):
    return (
        f"Generate today's daily report. "
        f"Today is {now.strftime('%A')}, {now.strftime('%d %B %Y')}, 8:00 AM Bangkok time "
        f"(Asia/Bangkok, UTC+7). "
        f"Use web search to find today's actual current news before writing, focusing on "
        f"stories published in the last 24 hours, and open articles to check facts. "
        f"The edition must contain all of its sections in order: {', '.join(REQUIRED_SECTIONS)}. "
        f"Keep each section to its usual length so the whole page fits comfortably in one answer. "
        f"Your final answer must be only the complete, valid HTML document, starting with "
        f"<!DOCTYPE html> and ending with </html>: no preamble, no notes, no markdown, no code fences."
        + (f"\n\nIMPORTANT: {feedback}" if feedback else '')
    )


def generate_briefing(now, system_prompt):
    feedback = ''
    for attempt in (1, 2):
        raw = claude_llm.ask(briefing_prompt(now, feedback), system=system_prompt,
                             tools=claude_llm.WEB_TOOLS, timeout=1500, attempts=2,
                             label=f'briefing #{attempt}')
        report = extract_html(raw.strip())
        problems = completeness_problems(report)
        print(f"Report extracted: {len(report)} characters")
        if not problems:
            return report
        print(f"Briefing attempt {attempt} incomplete: {'; '.join(problems)}")
        feedback = ("Your previous answer was rejected because it " + '; '.join(problems) +
                    ". Write the complete page again, keeping every section concise.")
    raise SystemExit("ERROR: briefing still incomplete after a retry; not publishing a broken page")


def main():
    bangkok_tz = ZoneInfo("Asia/Bangkok")
    now = datetime.now(bangkok_tz)
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
