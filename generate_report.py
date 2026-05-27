from google import genai
from google.genai import types
import os
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

# Initialize Gemini client
client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
system_prompt = os.environ['SYSTEM_PROMPT']

# Auto-select latest Gemini Flash model
def get_latest_gemini():
    try:
        models = client.models.list()
        flash_models = [
            m.name for m in models
            if 'gemini' in m.name.lower()
            and 'flash' in m.name.lower()
            and 'preview' not in m.name.lower()
            and 'thinking' not in m.name.lower()
        ]
        if flash_models:
            latest = sorted(flash_models, reverse=True)[0]
            model_id = latest.replace('models/', '')
            print(f"Using model: {model_id}")
            return model_id
    except Exception as e:
        print(f"Model fetch failed: {e}, falling back to default")
    return "gemini-2.0-flash"

MODEL = get_latest_gemini()

# Build the prompt
prompt = (
    f"Generate today's daily report. "
    f"Today is {bangkok_day}, {bangkok_date}, 8:00 AM Bangkok time (Asia/Bangkok, UTC+7). "
    f"Use Google Search to find today's actual current news before writing. "
    f"Search for stories published in the last 24 hours. "
    f"Output only a complete valid HTML document starting with <!DOCTYPE html>. "
    f"No preamble, no thinking text, no markdown, no code fences — just the HTML."
)

print("Calling Gemini API with Google Search grounding...")

try:
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            max_output_tokens=8000,
            temperature=0.7,
        )
    )
    raw = response.text
    print(f"Response received: {len(raw)} characters")

except Exception as e:
    print(f"ERROR: Gemini API call failed: {e}")
    exit(1)

# Strip anything before the HTML document
if '<!DOCTYPE html>' in raw:
    report = raw[raw.index('<!DOCTYPE html>'):]
elif '<html' in raw:
    report = raw[raw.index('<html'):]
else:
    report = raw

print(f"Report extracted: {len(report)} characters")

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
