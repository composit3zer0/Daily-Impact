"""Thin wrapper around headless Claude Code (`claude -p`) for the Daily Impact.

Authentication: ANTHROPIC_API_KEY, an API key from the Claude Console organization
linked to the owner's Max plan, so usage is billed to the plan's monthly API credit
under the Commercial Terms. If no key is set, Claude Code falls back to
CLAUDE_CODE_OAUTH_TOKEN (a subscription token from `claude setup-token`).

Model: CLAUDE_MODEL (default "sonnet"). The alias always points at the current
Sonnet, so a retired model name can't break the job the way Gemini's did.
"""

import json
import os
import subprocess
import tempfile
import time

DEFAULT_MODEL = os.environ.get('CLAUDE_MODEL', '').strip() or 'sonnet'
CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')
WEB_TOOLS = ('WebSearch', 'WebFetch')


class ClaudeError(RuntimeError):
    pass


def ask(prompt, *, system=None, schema=None, tools=(), allowed=None, model=None,
        timeout=900, attempts=2, label='claude', cwd=None):
    """Run one headless Claude Code request and return its answer.

    With `schema` (a JSON Schema dict) the answer is the validated
    structured_output object; otherwise it is the final text.
    `tools` lists the built-in tools Claude may use (e.g. WEB_TOOLS); by
    default it has none, so it can't touch files or run commands. `allowed`
    narrows what runs without a prompt (e.g. "Bash(python3 tools/selftest.py)");
    anything else is denied, since the mode is dontAsk.
    """
    model = model or DEFAULT_MODEL
    cmd = [CLAUDE_BIN, '-p', '--model', model,
           '--output-format', 'json', '--no-session-persistence',
           '--permission-mode', 'dontAsk',
           '--tools', ','.join(tools)]
    if tools:
        cmd += ['--allowedTools', ','.join(allowed if allowed is not None else tools)]
    if schema is not None:
        cmd += ['--json-schema', json.dumps(schema)]
    sys_file = None
    if system:
        sys_file = tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8')
        sys_file.write(system)
        sys_file.close()
        cmd += ['--system-prompt-file', sys_file.name]

    if cwd is None:
        # Writing calls get no file tools; run them from an empty folder so the repo's
        # CLAUDE.md (maintainer notes) isn't loaded into the briefing's context.
        cwd = tempfile.mkdtemp(prefix='daily-impact-')
    env = dict(os.environ)
    if not env.get('ANTHROPIC_API_KEY'):
        env.pop('ANTHROPIC_API_KEY', None)      # an empty secret must not block the token fallback
    last = None
    try:
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                      timeout=timeout, env=env, cwd=cwd)
            except subprocess.TimeoutExpired:
                last = ClaudeError(f'{label}: timed out after {timeout}s')
                print(f'[{label}] attempt {attempt} timed out')
                continue
            took = time.monotonic() - started
            try:
                data = json.loads(proc.stdout)
            except ValueError:
                last = ClaudeError(f'{label}: exit {proc.returncode}, unreadable output: '
                                   f'{(proc.stderr or proc.stdout)[-500:]}')
                print(f'[{label}] attempt {attempt} failed: {last}')
                time.sleep(15)
                continue
            models = [m for m in data.get('modelUsage', {}) if 'haiku' not in m] or list(data.get('modelUsage', {}))
            print(f"[{label}] {data.get('subtype')} in {took:.0f}s on {', '.join(models) or model}"
                  f" (est. ${data.get('total_cost_usd', 0):.2f})")
            if data.get('is_error') or data.get('subtype') != 'success':
                last = ClaudeError(f"{label}: {data.get('subtype')}: {str(data.get('result'))[:500]}")
            elif schema is not None:
                if data.get('structured_output') is not None:
                    return data['structured_output']
                last = ClaudeError(f'{label}: no structured output returned')
            elif data.get('result'):
                return data['result']
            else:
                last = ClaudeError(f'{label}: empty answer')
            print(f'[{label}] attempt {attempt} failed: {last}')
            time.sleep(15)
        raise last or ClaudeError(f'{label}: failed')
    finally:
        if sys_file:
            os.unlink(sys_file.name)
