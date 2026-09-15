"""Publishing a package pin does not make its verified desktop build stale."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('changed,accepted', [
    ('os/overlay/app-misc/posterchan-desktop/Manifest', True),
    ('static/js/client/cord-call.js', False),
    ('tests/client/test_cord_voice.py', False),
    ('.github/workflows/desktop.yml', False),
])
def test_ancestor_release_requires_only_overlay_changes(tmp_path, monkeypatch, changed, accepted):
    spec = importlib.util.spec_from_file_location('overlay_followup', ROOT / 'scripts/bump_desktop_overlay.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (tmp_path / 'source').write_text('released source')
    git('add', '.')
    git('commit', '-qm', 'released')
    released = git('rev-parse', 'HEAD')
    path = tmp_path / changed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('followup')
    git('add', '.')
    git('commit', '-qm', 'followup')
    head = git('rev-parse', 'HEAD')
    monkeypatch.setattr(module, 'ROOT', str(tmp_path))
    monkeypatch.setattr(module, '_releases', lambda: [('1.0.42', released)])
    assert module._tag_for_commit(head) == ('1.0.42' if accepted else None)
    # A build from a descendant/other history is never substituted for this tree.
    monkeypatch.setattr(module, '_releases', lambda: [('1.0.43', head)])
    assert module._tag_for_commit(released) is None
