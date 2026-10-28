from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_reconciles_completed_desktop_release_before_commit_and_publish():
    sync = (ROOT / "sync.sh").read_text()
    check = sync.index("scripts/bump_desktop_overlay.py --check")
    commit = sync.index("git commit -a -m fix")
    publish = sync.index("./scripts/publish_overlay.sh")
    assert check < commit < publish
    assert "scripts/bump_desktop_overlay.py\n" in sync


def _run_bump_block(tmp_path, venv):
    """Runs sync.sh's own bump block in a tree that has only `venv` and a stub python recording its calls."""
    import subprocess
    sync = (ROOT / "sync.sh").read_text()
    start = sync.index("# venv-unified is this deployment's venv") if "# venv-unified is this" in sync else \
        sync.index("if [ -x .venv/bin/python ] && ! .venv/bin/python scripts/bump_desktop_overlay.py --check")
    end = sync.index("\nfi\n", start) + 4
    py = tmp_path / venv / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\necho "$@" >> calls\nfor a; do [ "$a" = --check ] && exit 1; done\nexit 0\n')
    py.chmod(0o755)
    subprocess.run(["bash", "-c", sync[start:end]], cwd=tmp_path, check=True, capture_output=True)
    return (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []


def test_the_bump_runs_with_the_venv_this_deployment_has(tmp_path):
    """Only .venv was checked, which no node has -- so the overlay was never bumped by a deploy."""
    calls = _run_bump_block(tmp_path, "venv-unified")
    assert calls == ["scripts/bump_desktop_overlay.py --check", "scripts/bump_desktop_overlay.py"], calls


def test_overlay_publisher_never_downgrades_from_the_mutable_rolling_feed():
    publish = (ROOT / "scripts/publish_overlay.sh").read_text()
    assert "latest-linux.yml" not in publish
    assert 'desktop-v${LIVE}' in publish
    assert "LIVE=$(basename \"$CUR\"" in publish


def test_overlay_publisher_preserves_history_when_public_http_briefly_404s():
    publish = (ROOT / "scripts/publish_overlay.sh").read_text()
    public = publish.index('git clone -q "$URL" "$TMP/repo"')
    authoritative = publish.index('git clone -q "ssh://$NAS$DEST" "$TMP/repo"')
    initialise = publish.index('git -C "$TMP/repo" init -q -b main')
    assert public < authoritative < initialise


def test_overlay_publisher_excludes_host_python_bytecode():
    publish = (ROOT / "scripts/publish_overlay.sh").read_text()
    assert "--exclude='__pycache__'" in publish
    assert "--exclude='*.pyc'" in publish
    assert 'cp -r "$SRC/." "$TMP/"' not in publish
