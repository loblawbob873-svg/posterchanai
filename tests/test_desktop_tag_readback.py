"""Execute Desktop's actual publish step against stale reads and real Git ancestry."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def step():
    workflow = yaml.safe_load((ROOT / '.github/workflows/desktop.yml').read_text())
    return next(item['run'] for item in workflow['jobs']['publish']['steps']
                if item.get('name') == 'Advance rolling release tag')


@pytest.fixture
def release(tmp_path):
    repo = tmp_path / 'repository'
    repo.mkdir()
    git = shutil.which('git')
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
           'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}

    def command(*args):
        return subprocess.check_output([git, *args], cwd=repo, env=env, text=True).strip()

    command('init', '-q')
    commits = {}
    for name in ['old', 'expected', 'new']:
        command('commit', '--allow-empty', '-qm', name)
        commits[name] = command('rev-parse', 'HEAD')
    command('checkout', '-q', '--orphan', 'unrelated')
    command('commit', '--allow-empty', '-qm', 'other history')
    commits['unrelated'] = command('rev-parse', 'HEAD')
    commands = tmp_path / 'commands'
    commands.mkdir()
    # Stub external I/O only. Git's ancestry algorithm still runs on actual commits.
    (commands / 'git').write_text('#!/bin/sh\n[ "$1" != fetch ] || exit 0\nexec ' + git + ' "$@"\n')
    (commands / 'sleep').write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$FAKE_SLEEPS"\n')
    (commands / 'gh').write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p=Path(os.environ['FAKE_STATE']); state=json.loads(p.read_text()); args=sys.argv[1:]
state['calls'].append(args)
result=0; output='{}'
if '--method' in args:
    result=state['write_error']
elif '--jq' in args:
    i=state['reads']; state['reads']+=1
    output=state['responses'][min(i,len(state['responses'])-1)]
    if output=='ERROR': result=17
else:
    result=0 if state['exists'] else 1
p.write_text(json.dumps(state))
print(output)
sys.exit(result)
''')
    for path in commands.iterdir():
        path.chmod(0o755)

    def run(responses, *, exists=True, write_error=0):
        state = tmp_path / 'state.json'
        state.write_text(json.dumps({'responses': [commits.get(x, x) for x in responses],
                                    'calls': [], 'reads': 0, 'exists': exists, 'write_error': write_error}))
        sleeps = tmp_path / 'sleeps'
        result = subprocess.run(['bash', '-c', step()], cwd=repo,
                                env={**env, 'PATH': str(commands) + os.pathsep + env['PATH'],
                                     'GITHUB_REPOSITORY': 'test/repo', 'GITHUB_SHA': commits['expected'],
                                     'FAKE_STATE': str(state), 'FAKE_SLEEPS': str(sleeps)},
                                capture_output=True, text=True, timeout=10)
        actual = json.loads(state.read_text())
        actual['sleeps'] = sleeps.read_text().splitlines() if sleeps.exists() else []
        actual['writes'] = [args for args in actual['calls'] if '--method' in args]
        return result, actual
    return run


def test_successful_patch_waits_for_stale_read_to_catch_up(release):
    result, state = release(['old', 'expected'])
    assert result.returncode == 0, result.stdout + result.stderr
    assert state['reads'] == 2
    assert state['sleeps'] == ['2']
    assert len(state['writes']) == 1, 'verification must never PATCH again and roll back a newer run'


def test_matching_tag_does_not_wait(release):
    result, state = release(['expected'])
    assert result.returncode == 0, result.stderr
    assert state['reads'] == 1
    assert state['sleeps'] == []


@pytest.mark.parametrize('responses', [['new'], ['old', 'new']])
def test_superseding_descendant_is_accepted(release, responses):
    result, state = release(responses)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'superseded' in result.stdout
    assert len(state['writes']) == 1


@pytest.mark.parametrize('response', ['old', 'unrelated', 'ERROR'])
def test_persistently_wrong_or_unreadable_tag_still_fails_after_bounded_wait(release, response):
    result, state = release([response])
    assert result.returncode != 0
    assert state['reads'] == 5
    assert state['sleeps'] == ['2', '4', '6', '8']
    assert len(state['writes']) == 1
    assert '::error::' in result.stdout


def test_transient_read_failure_is_retried(release):
    result, state = release(['ERROR', 'expected'])
    assert result.returncode == 0, result.stdout + result.stderr
    assert state['reads'] == 2


def test_failed_patch_is_not_mistaken_for_eventual_read_consistency(release):
    result, state = release(['expected'], write_error=23)
    assert result.returncode == 23
    assert state['reads'] == 0
    assert state['sleeps'] == []


def test_missing_rolling_tag_is_created_then_verified(release):
    result, state = release(['expected'], exists=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(state['writes']) == 1
    assert state['writes'][0][state['writes'][0].index('--method') + 1] == 'POST'
    assert state['reads'] == 1
