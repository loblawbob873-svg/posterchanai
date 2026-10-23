"""scripts/watch_app_builds.py: a deploy's apps are followed until they are PUBLISHED, and sync.sh says so.

On 2026-10-22 Desktop apps failed every run from 18:06Z and one emulator test blocked an APK while every deploy
reported green; the Windows app ran a build from before all of that day's fixes. The verdict is driven here against
a stubbed `gh` answering in GitHub's own JSON shapes.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('watch_app_builds', ROOT / 'scripts' / 'watch_app_builds.py')
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

STARTED = '2026-10-22T22:48:23Z'


def run(name, conclusion='success', status='completed', rid=1):
    return {'databaseId': rid, 'name': name, 'status': status, 'conclusion': conclusion, 'createdAt': STARTED,
            'attempt': 1, 'url': 'https://github.com/x/runs/%d' % rid}


@pytest.fixture
def gh(monkeypatch, tmp_path):
    state = {'runs': [], 'releases': {}, 'jobs': {}, 'down': False}

    def fake(*args):
        if state['down']:
            raise RuntimeError('error connecting to api.github.com')
        if args[:2] == ('run', 'list'):
            return state['runs']
        if args[:2] == ('run', 'view'):
            return {'jobs': state['jobs'].get(args[2], [])}
        if args[:2] == ('release', 'view'):
            return state['releases'][args[2]]
        raise AssertionError(args)

    monkeypatch.setattr(W, 'gh', fake)
    monkeypatch.setattr(W, 'VERDICT', tmp_path / 'verdict.json')
    monkeypatch.setattr(W, 'TRIGGER_WAIT', 0)
    monkeypatch.setattr(W.time, 'sleep', lambda s: None)
    return state


def rel(when):
    return {'publishedAt': '2026-10-22T17:18:51Z', 'createdAt': '2026-10-22T17:18:51Z',
            'assets': [{'updatedAt': when}]}


def test_every_app_published_is_ok(gh):
    gh['runs'] = [run('Android APK', rid=1), run('Android emulator checks', rid=2), run('Desktop apps', rid=3)]
    gh['releases'] = {'apk-latest': rel('2026-10-22T23:30:00Z'), 'desktop-latest': rel('2026-10-22T23:40:00Z')}
    got = W.watch('a' * 40)
    assert got['ok'], got
    assert got['apps']['Desktop apps'] == {'state': 'published', 'release': 'desktop-latest', 'at': '2026-10-22 23:40Z'}
    assert json.loads(W.VERDICT.read_text())['ok'] is True


def test_a_failed_build_names_the_step(gh):
    gh['runs'] = [run('Android APK', rid=1), run('Android emulator checks', rid=2),
                  run('Desktop apps', 'failure', rid=3)]
    gh['releases'] = {'apk-latest': rel('2026-10-22T23:30:00Z')}
    gh['jobs'] = {'3': [{'name': 'linux', 'conclusion': 'failure', 'steps': [
        {'name': 'Install desktop regression packages', 'conclusion': 'failure'}]}]}
    got = W.watch('a' * 40)
    assert not got['ok']
    assert got['apps']['Desktop apps']['state'] == 'FAILED'
    assert 'linux / Install desktop regression packages' in got['apps']['Desktop apps']['why']
    assert W.main(['x', '--last']) == 1


def test_a_green_run_whose_release_never_moved_is_not_published(gh):
    gh['runs'] = [run('Android APK', rid=1)]
    gh['releases'] = {'apk-latest': rel('2026-10-22T21:49:48Z')}      # before the run started
    got = W.watch('a' * 40)
    assert got['apps']['Android APK']['state'] == 'FAILED' and not got['ok'], got


def test_a_workflow_its_path_filter_skipped_is_not_a_failure(gh):
    gh['runs'] = [run('Android APK', rid=1), run('Android emulator checks', rid=2)]
    gh['releases'] = {'apk-latest': rel('2026-10-22T23:30:00Z')}
    got = W.watch('a' * 40)
    assert got['apps']['Desktop apps']['state'] == 'not triggered'
    assert got['ok'], got


def test_github_unreachable_is_unknown_never_ok(gh, monkeypatch):
    gh['down'] = True
    monkeypatch.setattr(W, 'DEADLINE', 0)
    got = W.watch('a' * 40)
    assert {a['state'] for a in got['apps'].values()} == {'unknown'} and not got['ok'], got


def test_sync_follows_the_builds_and_reports_the_last_verdict():
    sync = (ROOT / 'sync.sh').read_text()
    start = sync.index('watch_app_builds.py --last')
    assert start < sync.index('deploy_regression_gate.py --verify'), 'the previous verdict is shown before the deploy'
    assert 'PIPESTATUS[0]' in sync[start:start + 400], "the watcher's status, not sed's"
    follow = re.search(r'nohup venv-unified/bin/python scripts/watch_app_builds.py "\$_EXPECT"[^\n]*\n[^\n]*&', sync)
    assert follow, 'the builds of the deployed commit are followed, detached'
    assert sync.index('git push github master:main') < follow.start()


def _overlay(monkeypatch, pinned, tags, gh_down=False):
    class R:
        stdout = f"os/overlay/app-misc/posterchan-desktop/Manifest\nos/overlay/app-misc/posterchan-desktop/posterchan-desktop-{pinned}.ebuild\n"
    monkeypatch.setattr(W.subprocess, 'run', lambda *a, **k: R())

    def fake(*args):
        if gh_down:
            raise RuntimeError('error connecting to api.github.com')
        assert args[:2] == ('release', 'list')
        return [{'tagName': t} for t in tags]
    monkeypatch.setattr(W, 'gh', fake)
    return W.overlay_freshness('a' * 40)


def test_the_verdict_says_when_the_gentoo_overlay_trails_the_newest_desktop(monkeypatch):
    got = _overlay(monkeypatch, '1.0.1993', ['desktop-latest', 'desktop-v1.0.1996', 'desktop-v1.0.1995', 'apk-latest'])
    assert got == {'state': 'behind', 'pinned': '1.0.1993', 'newest': '1.0.1996'}
    # numeric, not lexical: 1.0.999 is older than 1.0.1000
    assert _overlay(monkeypatch, '1.0.999', ['desktop-v1.0.1000'])['state'] == 'behind'
    assert _overlay(monkeypatch, '1.0.1996', ['desktop-v1.0.1996', 'desktop-v1.0.1995'])['state'] == 'current'


def test_an_unreadable_release_list_is_unknown_never_current(monkeypatch):
    assert _overlay(monkeypatch, '1.0.1993', [], gh_down=True)['state'] == 'unknown'
