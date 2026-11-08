"""Offline self-tests for the Daily Impact pipeline (no network, no API calls).

Run:  python3 tools/selftest.py
The self-repair workflow runs this before merging any automatic fix, so keep it
fast and deterministic. Exit code 0 = all tests passed.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import claude_llm            # noqa: E402
import essential_reading as er  # noqa: E402
import generate_report as gr     # noqa: E402

NOW = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)
RESULTS = []


def test(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, None))
    except Exception:  # noqa: BLE001
        RESULTS.append((fn.__name__, traceback.format_exc()))
    return fn


def page(sections=gr.REQUIRED_SECTIONS, closed=True):
    body = ''.join(f'<div class="card"><div class="section-header">{s}</div><p>{"text " * 300}</p></div>'
                   for s in sections)
    html = f'<!DOCTYPE html><html><head><title>t</title></head><body><div class="page">{body}</div>'
    return html + ('</body></html>' if closed else '')


# ----------------------------------------------------------------- briefing

@test
def briefing_complete_page_passes():
    assert gr.completeness_problems(page()) == []


@test
def briefing_cut_off_page_is_rejected():
    probs = gr.completeness_problems(page(sections=['News', 'Editorial'], closed=False))
    assert any('</html>' in p for p in probs), probs
    assert any('Dhamma Corner' in p for p in probs) and any('Personal Reflection' in p for p in probs), probs


@test
def briefing_preamble_is_stripped():
    assert gr.extract_html('Here you go:\n' + page()).startswith('<!DOCTYPE html>')


@test
def briefing_retries_once_then_refuses_to_publish():
    calls = []
    real = claude_llm.ask
    claude_llm.ask = lambda prompt, **kw: calls.append(prompt) or page(sections=['News'], closed=False)
    try:
        try:
            gr.generate_briefing(NOW, 'system')
            raise AssertionError('incomplete briefing was accepted')
        except SystemExit as e:
            assert 'not publishing' in str(e)
        assert len(calls) == 2 and 'rejected' in calls[1]
    finally:
        claude_llm.ask = real


# --------------------------------------------------------------- reading

@test
def article_checks_parse_dates_and_paywalls():
    from bs4 import BeautifulSoup
    h = ('<html><head><script type="application/ld+json">{"@graph":[{"datePublished":'
         '"2026-11-01T08:00:00+07:00","isAccessibleForFree":"False"}]}</script></head><body></body></html>')
    soup = BeautifulSoup(h, 'html.parser')
    d, src = er.extract_published(soup)
    assert d == datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc) and src == 'json-ld'
    assert er.detect_paywall(soup, h)


@test
def focus_keywords_match_conditions_not_lookalikes():
    cfg = er.load_json(er.SOURCES_FILE, {})
    spec = er.load_focus(cfg)[3]
    assert er.focus_topics(spec, 'Adults with ADHD at work') == ['ADHD']
    assert er.focus_topics(spec, 'Veterans living with C-PTSD') == ['PTSD']
    assert er.focus_topics(spec, 'Mania for crypto') == []


@test
def reading_pipeline_end_to_end_with_stub_claude():
    tmp = tempfile.mkdtemp()
    saved = (er.HERE, er.HISTORY_FILE, er.fetch_feed, er.HTTP.get, claude_llm.ask)
    try:
        er.HISTORY_FILE = os.path.join(tmp, 'reading_history.json')
        er.HERE = tmp
        with open(er.HISTORY_FILE, 'w') as f:
            json.dump([], f)

        def fake_feed(url, now=None):
            slug = re.sub(r'\W+', '-', url)[-30:]
            title = 'New thinking on ADHD' if 'adhd' in url else 'Article'
            return [{'title': f'{title} {i} {slug}', 'url': f'https://{er.domain_of(url)}/a/{slug}-{i}',
                     'published': NOW - timedelta(days=i + 1), 'summary': 'teaser'} for i in range(2)], None

        class R:
            def __init__(self, url):
                self.url, self.status_code = url, 200
                self.text = ('<html><head><meta property="og:title" content="T"><script type="application/'
                             'ld+json">{"datePublished":"2026-11-06T00:00:00Z"}</script></head><body><article><p>'
                             + 'word ' * 2400 + '</p></article></body></html>')

        def fake_ask(prompt, schema=None, **kw):
            if 'Step 1 of 2' in prompt:
                out, taken = {}, set()
                for n in range(1, 8):
                    block = prompt.split(f'## Category {n}:')[1].split('## Category')[0]
                    ids = [i for i in re.findall(r'^(F\d+) \|', block, re.M) if i not in taken][:3]
                    taken.update(ids)
                    out[str(n)] = ids
                return out
            if 'Step 2 of 2' in prompt:
                out = {}
                for n in schema['required']:
                    block = prompt.split(f'## Category {n}:')[1].split('## Category')[0]
                    ids = re.findall(r'^### ([FS]\d+) \|', block, re.M)
                    out[n] = {'id': ids[0], 'summary': ' '.join(['insight'] * 100)} if ids else {'id': None, 'reason': 'none'}
                return out
            return {'urls': []}

        er.fetch_feed, er.HTTP.get, claude_llm.ask = fake_feed, lambda url, **kw: R(url), fake_ask
        out = er.add_essential_reading(page(), NOW)
        sec = out[out.index("Today's Essential Reading"):]
        assert out.index("Today's Essential Reading") < out.index('Personal Reflection'), 'section misplaced'
        assert sec.count('<a href') == 7, f"expected 7 picks, got {sec.count('<a href')}"
        hist = json.load(open(er.HISTORY_FILE))
        assert len(hist) == 7 and len({h['url'] for h in hist}) == 7, 'picks must be unique'
        assert any(h['focus'] for h in hist if h['category'] == 3), 'category 3 must be a focus pick'
    finally:
        er.HERE, er.HISTORY_FILE, er.fetch_feed, er.HTTP.get, claude_llm.ask = saved
        shutil.rmtree(tmp, ignore_errors=True)


@test
def reading_failure_never_breaks_the_briefing():
    real, real_hist = claude_llm.ask, er.HISTORY_FILE
    tmp = tempfile.mkdtemp()
    try:
        er.HISTORY_FILE = os.path.join(tmp, 'h.json')

        def boom(*a, **k):
            raise claude_llm.ClaudeError('down')
        claude_llm.ask = boom
        saved_feed = er.fetch_feed
        er.fetch_feed = lambda url, now=None: ([], 'offline')
        out = er.add_essential_reading(page(), NOW)
        er.fetch_feed = saved_feed
        assert 'Personal Reflection' in out and out.count('No recommendation today') == 7
    finally:
        claude_llm.ask, er.HISTORY_FILE = real, real_hist
        shutil.rmtree(tmp, ignore_errors=True)


@test
def section_is_inserted_before_personal_reflection():
    out = er.insert_section(page(), '<div class="card essential-reading">X</div>')
    assert out.index('essential-reading') < out.index('Personal Reflection')


@test
def source_list_is_valid():
    cfg = er.load_json(er.SOURCES_FILE, None)
    assert cfg and cfg['sources'], 'reading_sources.json missing or empty'
    for s in cfg['sources']:
        for url, cats in er.feeds_of(s):
            assert url.startswith('https://') and set(cats) <= set(range(1, 8)), (s['name'], url)


if __name__ == '__main__':
    os.chdir(ROOT)
    failed = [(n, tb) for n, tb in RESULTS if tb]
    for name, tb in RESULTS:
        print(('FAIL ' if tb else 'ok   ') + name)
        if tb:
            print(tb)
    print(f'{len(RESULTS) - len(failed)}/{len(RESULTS)} passed')
    sys.exit(1 if failed else 0)
