#!/usr/bin/env python3
"""Did this deploy's APPS get published? Follows one commit's release builds on the github mirror to the end.

`sync.sh` is finished once every node and both remotes are on the commit, but the APK and the desktop apps are built
~40 minutes later by GitHub Actions on the mirror -- and for most of 2026-10-22 they were not built at all (Desktop
apps failed every run from 18:06Z: an apt JDK install that stalled; one Android emulator test blocked an APK) while
every deploy reported green. The Windows app kept running a build from before all of that day's fixes and nothing
anywhere said so. sync.sh now starts this detached after a deploy and prints its verdict at the start of the next.

  watch_app_builds.py <sha>            wait for the runs (up to ~90 min), write the verdict, exit 0 ok / 1 failed
  watch_app_builds.py --last           print the last verdict (what sync.sh shows), exit 1 if it was a failure

A workflow whose path filter did not fire for this commit is NOT a failure and is reported as "not triggered";
"could not ask GitHub" is "unknown", never "ok". A green run is not proof of publication by itself, so the
rolling release it publishes (apk-latest / desktop-latest) must also have moved since the run started.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = 'loblawbob873-svg/posterchanai'
VERDICT = Path.home() / '.cache' / 'posterchanai' / 'app-builds.json'
# workflow name -> the rolling release a green run must have moved (None: a gate, publishes nothing itself)
WORKFLOWS = {'Android APK': 'apk-latest', 'Android emulator checks': None, 'Desktop apps': 'desktop-latest'}
TRIGGER_WAIT = 300      # a push starts its runs within seconds; past this a missing run is a path filter
DEADLINE = 90 * 60


def gh(*args):
    r = subprocess.run(['gh', *args, '-R', REPO], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return json.loads(r.stdout or 'null')


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def runs_for(sha):
    out = {}
    for run in gh('run', 'list', '-c', sha, '-L', '30', '--json',
                  'databaseId,name,status,conclusion,createdAt,attempt,url'):
        if run['name'] in WORKFLOWS and (run['name'] not in out or run['databaseId'] > out[run['name']]['databaseId']):
            out[run['name']] = run
    return out


def failed_steps(run_id):
    try:
        jobs = gh('run', 'view', str(run_id), '--json', 'jobs')['jobs']
    except Exception as e:     # the verdict stands without the detail
        return ['(could not read the jobs: %s)' % e]
    return ['%s / %s' % (j['name'], s['name']) for j in jobs if j.get('conclusion') == 'failure'
            for s in j.get('steps', []) if s.get('conclusion') == 'failure'] or ['(no failing step reported)']


def release_moved(tag, since):
    try:
        rel = gh('release', 'view', tag, '--json', 'publishedAt,createdAt,assets')
    except Exception as e:
        return None, 'could not read release %s: %s' % (tag, e)
    newest = max([ts(a['updatedAt']) for a in rel.get('assets') or [] if a.get('updatedAt')] +
                 [ts(rel.get('publishedAt') or rel['createdAt'])])
    return newest >= since, datetime.fromtimestamp(newest, timezone.utc).strftime('%Y-%m-%d %H:%MZ')


def watch(sha):
    t0 = time.time()
    while True:
        try:
            runs = runs_for(sha)
        except Exception as e:
            runs, err = None, str(e)
        waited = time.time() - t0
        if runs is not None:
            pending = [n for n in WORKFLOWS if n in runs and runs[n]['status'] != 'completed']
            missing = [n for n in WORKFLOWS if n not in runs]
            if not pending and (not missing or waited > TRIGGER_WAIT):
                break
        if waited > DEADLINE:
            break
        time.sleep(30)

    result = {'sha': sha, 'checked': time.strftime('%Y-%m-%d %H:%M:%S'), 'apps': {}}
    if runs is None:
        result['apps'] = {n: {'state': 'unknown', 'why': 'could not ask GitHub: ' + err} for n in WORKFLOWS}
    for name, tag in WORKFLOWS.items():
        if runs is None:
            continue
        run = runs.get(name)
        if not run:
            result['apps'][name] = {'state': 'not triggered', 'why': 'no run for this commit (path filter)'}
        elif run['status'] != 'completed':
            result['apps'][name] = {'state': 'unknown', 'why': 'still %s after %d min' % (run['status'], DEADLINE // 60),
                                    'url': run['url']}
        elif run['conclusion'] != 'success':
            result['apps'][name] = {'state': 'FAILED', 'why': '; '.join(failed_steps(run['databaseId'])),
                                    'url': run['url']}
        elif tag:
            moved, when = release_moved(tag, ts(run['createdAt']))
            result['apps'][name] = ({'state': 'published', 'release': tag, 'at': when} if moved else
                                    {'state': 'unknown' if moved is None else 'FAILED', 'url': run['url'],
                                     'why': when if moved is None else
                                     'run passed but %s was last updated %s, before it started' % (tag, when)})
        else:
            result['apps'][name] = {'state': 'passed'}
    states = [a['state'] for a in result['apps'].values()]
    result['ok'] = 'FAILED' not in states and 'unknown' not in states
    VERDICT.parent.mkdir(parents=True, exist_ok=True)
    VERDICT.write_text(json.dumps(result, indent=1))
    return result


def show(result):
    head = 'apps for %s (checked %s): %s' % (result['sha'][:9], result['checked'],
                                             'OK' if result['ok'] else 'NOT ALL PUBLISHED')
    lines = [head] + ['  %-24s %s%s%s' % (n, a['state'], (' ' + a['at']) if a.get('at') else '',
                                         (' -- ' + a['why']) if a.get('why') else '') +
                      ('\n  %-24s %s' % ('', a['url']) if a.get('url') and a['state'] != 'published' else '')
                      for n, a in result['apps'].items()]
    print('\n'.join(lines))


def main(argv):
    if argv[1:] == ['--last']:
        if not VERDICT.exists():
            print('no app-build verdict recorded yet')
            return 0
        result = json.loads(VERDICT.read_text())
        show(result)
        return 0 if result['ok'] else 1
    if len(argv) != 2:
        print(__doc__)
        return 2
    result = watch(argv[1])
    show(result)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
