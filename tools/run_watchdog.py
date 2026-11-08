"""Watchdog for the Daily Impact Report workflow.

Opens (or updates) a GitHub issue labelled `daily-impact-alert` when a run
needs attention, and closes it when a healthy run follows. GitHub emails the
repository owner about new issues. For a failed or degraded run, the workflow's
self-repair job (tools/self_repair.py) then asks Claude to fix the code.

Modes (run by .github/workflows/run-watchdog.yml):
  after-run   right after each Daily Impact Report run. Alerts on a failed or
              cancelled run, or a successful run whose Essential Reading
              section is missing or too thin.
  daily       safety net. Alerts when there is no successful run on main
              today (UTC) or the workflow has been disabled.

Uses the `gh` CLI with GH_TOKEN; needs `issues: write` and `actions: read`.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

REPO = os.environ.get('GITHUB_REPOSITORY', 'composit3zer0/Daily-Impact')
WORKFLOW = 'daily-report.yml'
LABEL = 'daily-impact-alert'
MIN_READING = 4          # alert when fewer of the 7 recommendations were published


def gh(*args, check=True):
    res = subprocess.run(['gh', *args, '--repo', REPO] if args[0] not in ('api',) else ['gh', *args],
                         capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


# ------------------------------------------------------------------ analysis

def analyse_log(log):
    """Problems found in a successful run's log (empty list = healthy)."""
    problems = []
    if 'WARNING: Essential Reading skipped' in log:
        problems.append('The Essential Reading step crashed and was skipped; the briefing published without it.')
    m = re.findall(r'\[reading\] section published with (\d)/7 recommendations', log)
    if not m:
        if not problems:
            problems.append('No Essential Reading result line in the log; the section may be missing.')
    elif int(m[-1]) < MIN_READING:
        problems.append(f'Only {m[-1]}/7 Essential Reading recommendations were published.')
    if re.search(r'\[reading\] build failed with', log):
        fails = sorted(set(re.findall(r'\[reading\] build failed with ([\w.-]+)', log)))
        problems.append(f"Reading model call failed for: {', '.join(fails)}.")
    return problems


def tail_of_log(log, lines=40):
    keep = [l for l in log.splitlines()
            if re.search(r'ERROR|Error|error:|failed|Attempt \d+ failed|\[reading\] (build failed|section|verified|cat \d+: no)', l)]
    return '\n'.join((keep or log.splitlines())[-lines:])


# -------------------------------------------------------------------- issues

def open_issue():
    out = gh('issue', 'list', '--label', LABEL, '--state', 'open', '--json', 'number', '--limit', '1')
    items = json.loads(out or '[]')
    return items[0]['number'] if items else None


def set_output(**values):
    """Tell later workflow steps/jobs about the alert (no-op outside Actions)."""
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a') as f:
            for k, v in values.items():
                f.write(f'{k}={v}\n')


def alert(title, body, repairable=False):
    gh('label', 'create', LABEL, '--color', 'B60205',
       '--description', 'Daily Impact run needs attention', check=False)
    number = open_issue()
    if number:
        gh('issue', 'comment', str(number), '--body', f'### {title}\n\n{body}')
        print(f'Commented on issue #{number}: {title}')
    else:
        url = gh('issue', 'create', '--title', f'Daily Impact alert: {title}', '--label', LABEL,
                 '--body', body).strip()
        number = int(url.rstrip('/').rsplit('/', 1)[-1]) if url.rstrip('/').rsplit('/', 1)[-1].isdigit() else open_issue()
        print(f'Opened issue #{number}: {title}')
    set_output(alert='true', issue=number or '', repairable='true' if repairable else 'false')


def resolve(run_url):
    number = open_issue()
    if number:
        gh('issue', 'comment', str(number), '--body', f'Recovered: healthy run {run_url}. Closing.')
        gh('issue', 'close', str(number))
        print(f'Closed issue #{number}')
    else:
        print('Healthy; no open alert.')


# --------------------------------------------------------------------- modes

def after_run():
    run_id = os.environ['RUN_ID']
    conclusion = os.environ.get('RUN_CONCLUSION', '')
    url = os.environ.get('RUN_URL', f'https://github.com/{REPO}/actions/runs/{run_id}')
    branch = os.environ.get('RUN_BRANCH', '')
    if branch and branch != 'main':
        print(f'Run on branch {branch}; ignoring.')
        return
    log = gh('run', 'view', run_id, '--log', check=False)
    if conclusion != 'success':
        failed = gh('run', 'view', run_id, '--json', 'jobs', '--jq',
                    '[.jobs[].steps[] | select(.conclusion=="failure") | .name] | join(", ")', check=False).strip()
        alert(f'run {conclusion or "did not succeed"}',
              f'Run: {url}\nConclusion: **{conclusion}**\nFailed step: {failed or "unknown"}\n\n'
              f'Relevant log lines:\n```\n{tail_of_log(log)}\n```', repairable=conclusion == 'failure')
        return
    if 'SKIP:' in log and '[reading] section published' not in log:
        print("Backup schedule skipped: today's edition already existed. Nothing to report.")
        return
    problems = analyse_log(log)
    if problems:
        alert('run degraded',
              f'Run: {url} succeeded, but:\n' + '\n'.join(f'- {p}' for p in problems) +
              f'\n\nRelevant log lines:\n```\n{tail_of_log(log)}\n```', repairable=True)
    else:
        resolve(url)


def daily():
    today = datetime.now(timezone.utc).date().isoformat()
    state = gh('api', f'repos/{REPO}/actions/workflows/{WORKFLOW}', '--jq', '.state', check=False).strip()
    if state and state != 'active':
        alert(f'workflow is {state}',
              f'The Daily Impact Report workflow state is **{state}**, so no editions will be produced. '
              f'Re-enable it under Actions -> Daily Impact Report -> Enable workflow.')
        return
    runs = json.loads(gh('run', 'list', '--workflow', WORKFLOW, '--branch', 'main', '--limit', '10',
                         '--json', 'databaseId,createdAt,status,conclusion,url') or '[]')
    todays = [r for r in runs if r['createdAt'][:10] == today]
    if any(r['conclusion'] == 'success' for r in todays):
        print('A successful run exists today.')
        return
    if any(r['status'] in ('queued', 'in_progress', 'waiting', 'pending') for r in todays):
        print('A run is still in progress; the after-run check will report on it.')
        return
    detail = '\n'.join(f"- {r['createdAt']} {r['status']}/{r['conclusion']} {r['url']}" for r in todays) or '- none'
    alert('no successful run today',
          f'No successful Daily Impact Report run on main for {today} (UTC).\nRuns today:\n{detail}')


if __name__ == '__main__':
    {'after-run': after_run, 'daily': daily}[sys.argv[1]]()
