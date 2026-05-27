import anthropic
import os
import json
import glob
from datetime import datetime
import pytz

# Bangkok date and time
bangkok_tz = pytz.timezone('Asia/Bangkok')
now = datetime.now(bangkok_tz)
bangkok_date = now.strftime('%d %B %Y')
bangkok_day = now.strftime('%A')
archive_filename = now.strftime('%Y-%m-%d') + '.html'

print(f"Generating Daily Impact for {bangkok_date}...")

# Initialize Claude client
client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
system_prompt = os.environ['SYSTEM_PROMPT']

# Initial message
messages = [
    {
        "role": "user",
        "content": (
            f"Generate today's daily report. "
            f"Today is {bangkok_day}, {bangkok_date}, 8:00 AM Bangkok time (Asia/Bangkok, UTC+7). "
            f"Use web search to find today's actual current news before writing. "
            f"Search for stories published in the last 24 hours."
        )
    }
]

# Agentic loop — handles web search tool calls
report = ""
max_iterations = 10
iteration = 0

while iteration < max_iterations:
    iteration += 1
    print(f"API call {iteration}...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        system=system_prompt,
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search"
            }
        ],
        messages=messages
    )

    print(f"Stop reason: {response.stop_reason}")

    # Collect any text blocks from this response
    text_blocks = [
        block.text
        for block in response.content
        if hasattr(block, 'text') and block.type == 'text'
    ]

    if response.stop_reason == "end_turn":
        report = '\n'.join(text_blocks)
        print(f"Report complete: {len(report)} characters")
        break

    if response.stop_reason == "tool_use":
        # Add assistant turn to history
        messages.append({
            "role": "assistant",
            "content": response.content
        })

        # Build tool results
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"  Web search: {getattr(block, 'input', {})}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Search executed."
                })

        # Add tool results and continue
        messages.append({
            "role": "user",
            "content": tool_results
        })
        continue

    # Any other stop reason — use what we have
    report = '\n'.join(text_blocks)
    break

# Validate we have output
if not report or len(report) < 500:
    print("ERROR: Report generation failed or output too short")
    exit(1)

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
