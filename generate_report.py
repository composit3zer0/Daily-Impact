from google import genai
from google.genai import types
import os
import glob
import time
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

# Model chain. Google renames and retires model aliases without notice:
# - 28 Aug 2026: auto-selecting from the model list picked 'gemini-omni-1.1-flash',
#   which only supports the Interactions API (400 INVALID_ARGUMENT on every call);
# - 7 Nov 2026: 'gemini-flash-lite-latest' started returning 404 NOT_FOUND.
# So the script tries a short list in order and moves to the next model at once when
# a model is missing or rejects generate_content. As a last resort it asks the API
# for flash models that declare generateContent support.
# To put a model first, set a repository variable named GEMINI_MODEL
# (Settings -> Secrets and variables -> Actions -> Variables).
MODEL_CHAIN = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash"]


def candidate_models():
    first = os.environ.get('GEMINI_MODEL', '').strip()
    return list(dict.fromkeys(([first] if first else []) + MODEL_CHAIN))


def discovered_models(exclude):
    """Flash models the API says support generateContent (newest-looking first)."""
    try:
        found = []
        for m in client.models.list():
            name = (m.name or '').replace('models/', '')
            actions = [a.lower() for a in (getattr(m, 'supported_actions', None) or [])]
            if ('flash' in name and 'generatecontent' in actions and name not in exclude
                    and not any(x in name for x in ('preview', 'thinking', 'tts', 'image', 'live', 'omni'))):
                found.append(name)
        return sorted(found, reverse=True)[:3]
    except Exception as e:
        print(f"Model discovery failed: {e}")
        return []


def model_unusable(err):
    """True when retrying this model can't help (missing model or unsupported method)."""
    text = str(err)
    return any(k in text for k in ('404', 'NOT_FOUND', '400 INVALID_ARGUMENT', 'only supports'))


# Build the prompt
prompt = (
    f"Generate today's daily report. "
    f"Today is {bangkok_day}, {bangkok_date}, 8:00 AM Bangkok time (Asia/Bangkok, UTC+7). "
    f"Use Google Search to find today's actual current news before writing. "
    f"Search for stories published in the last 24 hours. "
    f"Output only a complete valid HTML document starting with <!DOCTYPE html>. "
    f"No preamble, no thinking text, no markdown, no code fences — just the HTML."
)


def generate_with(model):
    """Up to 3 attempts with one model. Returns (text, unusable)."""
    for attempt in range(1, 4):
        try:
            print(f"API call attempt {attempt}/3 with {model}...")
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    max_output_tokens=16384,
                    temperature=0.7,
                )
            )
            text = response.text
            if text:
                print(f"Response received: {len(text)} characters")
                return text, False
            print(f"Attempt {attempt}: empty response")
        except Exception as e:
            print(f"Attempt {attempt} failed: {e}")
            if model_unusable(e):
                print(f"{model} is unavailable for generate_content; trying the next model.")
                return None, True
        if attempt < 3:
            time.sleep(10)
    return None, False


print("Calling Gemini API with Google Search grounding...")

raw, MODEL, tried = None, None, []
for model in candidate_models():
    tried.append(model)
    raw, _ = generate_with(model)
    if raw:
        MODEL = model
        break
if not raw:
    for model in discovered_models(set(tried)):
        print(f"Trying discovered model: {model}")
        tried.append(model)
        raw, _ = generate_with(model)
        if raw:
            MODEL = model
            break

if not raw:
    print(f"ERROR: no model produced a report (tried: {', '.join(tried)})")
    exit(1)
print(f"Using model: {MODEL}")

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

# Add "Today's Essential Reading". This step never blocks the main report:
# on any failure the briefing is published without the section.
try:
    from essential_reading import add_essential_reading
    report = add_essential_reading(client, report, now, fallback_model=MODEL)
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
