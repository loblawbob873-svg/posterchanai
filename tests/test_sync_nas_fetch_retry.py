"""Execute sync.sh's actual SSH payload without network, services, or a real checkout."""
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path, failures=0, reset_fails=False, targets='posterchanai-worker.service'):
    source = (ROOT / 'sync.sh').read_text()
    start = source.index('ssh nas.lan "\n')
    end = source.index('\n"', start) + 2
    # Let Bash perform the shipped local quoting, then run the resulting remote script locally.
    payload = source[start:end]
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    checkout = tmp_path / 'posterchanai'
    checkout.mkdir()
    (checkout / 'keep-untracked').write_text('user data')
    programs = {
        'git': '''#!/bin/bash
printf 'git %s\\n' "$*" >> "$TEST_LOG"
case "$1" in
 rev-parse) echo previous-head ;;
 fetch)
   n=$(cat "$TEST_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$TEST_COUNT"
   [ "$n" -gt "$TEST_FAILURES" ] ;;
 reset) [ "$TEST_RESET_FAILS" = 0 ] ;;
 *) exit 99 ;;
esac
''',
        'python3': '#!/bin/bash\nprintf "targets %s\\n" "$*" >> "$TEST_LOG"\nprintf "%s\\n" "$TEST_TARGETS"\n',
        'systemctl': '''#!/bin/bash
printf 'systemctl %s\\n' "$*" >> "$TEST_LOG"
[ "$1" != is-enabled ]
''',
        'sudo': '#!/bin/bash\nexec "$@"\n',
        'flock': '#!/bin/bash\nprintf "flock %s\\n" "$*" >> "$TEST_LOG"\n',
        'sleep': '#!/bin/bash\nprintf "sleep %s\\n" "$*" >> "$TEST_LOG"\n',
    }
    for name, text in programs.items():
        path = bindir / name
        path.write_text(text)
        path.chmod(0o755)
    env = {**os.environ, 'PATH':str(bindir)+os.pathsep+os.environ['PATH'],
           'HOME':str(tmp_path), 'TEST_LOG':str(tmp_path/'calls'),
           'TEST_COUNT':str(tmp_path/'count'), 'TEST_FAILURES':str(failures),
           'TEST_RESET_FAILS':str(int(reset_fails)), 'TEST_TARGETS':targets}
    result = subprocess.run(['bash', '-c', 'ssh() { test "$1" = nas.lan || return 99; bash -c "$2"; };\n'+payload],
                            env=env, capture_output=True, text=True, timeout=5)
    assert (checkout/'keep-untracked').read_text() == 'user data'
    return result, (tmp_path/'calls').read_text().splitlines()


def test_transient_fetch_retries_before_reset_and_only_restarts_selected_unit(tmp_path):
    result, calls = _run(tmp_path, failures=1)
    assert result.returncode == 0, result.stderr
    assert calls[:5] == ['git rev-parse HEAD', 'git fetch origin', 'sleep 3',
                         'git fetch origin', 'git reset --hard origin/master']
    assert 'targets scripts/deploy_targets.py previous-head..HEAD' in calls
    assert [c for c in calls if c.startswith('systemctl restart')] == [
        'systemctl restart posterchanai-worker.service']
    assert any(c.startswith('flock ') for c in calls)


def test_exhausted_fetch_stops_before_reset_target_resolution_or_services(tmp_path):
    result, calls = _run(tmp_path, failures=99)
    assert result.returncode != 0
    assert 'ABORT: fetch failed after 5 attempts' in result.stderr
    assert calls.count('git fetch origin') == 5
    assert calls.count('sleep 3') == 4
    assert all(c == 'git rev-parse HEAD' or c == 'git fetch origin' or c == 'sleep 3' for c in calls)


def test_reset_failure_stops_before_target_resolution_or_services(tmp_path):
    result, calls = _run(tmp_path, reset_fails=True)
    assert result.returncode != 0
    assert calls == ['git rev-parse HEAD', 'git fetch origin', 'git reset --hard origin/master']


def test_successful_fetch_without_targets_does_not_wait_for_gpu_or_restart(tmp_path):
    result, calls = _run(tmp_path, targets='')
    assert result.returncode == 0, result.stderr
    assert calls.count('git fetch origin') == 1
    assert not any(c.startswith(('sleep ', 'flock ', 'systemctl restart')) for c in calls)
