"""Real competing processes must not run two release suites in the same repository."""
import subprocess
import sys

import pytest
from scripts import checkall


def test_lock_refuses_a_competitor_and_releases_after_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(checkall, 'ROOT', tmp_path)
    code = ('from pathlib import Path; from scripts import checkall; '
            'import sys,time; checkall.ROOT=Path(sys.argv[1]); '
            'lock=checkall._runner_lock(); lock.__enter__(); '
            'print("locked", flush=True); time.sleep(30)')
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # communicate has a bound even if acquiring the lock fails unexpectedly.
        import select
        assert select.select([child.stdout], [], [], 10)[0], 'lock owner never started'
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(checkall.RunnerBusy, match=f'PID {child.pid}'):
            with checkall._runner_lock():
                pytest.fail('two runners acquired the same lock')
        monkeypatch.setattr(sys, 'argv', ['checkall', '--only', 'tests', '--tmp', str(tmp_path / 'logs')])
        monkeypatch.setattr(checkall, '_execute', lambda *a: pytest.fail('busy runner started tests'))
        assert checkall.main() == 2
        monkeypatch.setattr(sys, 'argv', ['checkall', '--list', '--tmp', str(tmp_path / 'logs')])
        assert checkall.main() == 0, 'listing must work during a running suite'
    finally:
        child.kill()
        child.communicate(timeout=5)
    with checkall._runner_lock():
        pass  # SIGKILL must not leave a stale lock behind.


def test_linked_worktrees_contend_for_the_same_lock(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    linked = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', str(linked)], check=True)
    monkeypatch.setattr(checkall, 'ROOT', repo)
    with checkall._runner_lock():
        monkeypatch.setattr(checkall, 'ROOT', linked)
        with pytest.raises(checkall.RunnerBusy):
            with checkall._runner_lock():
                pytest.fail('a worktree bypassed the running suite')
