from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_reconciles_completed_desktop_release_before_commit_and_publish():
    sync = (ROOT / "sync.sh").read_text()
    check = sync.index("scripts/bump_desktop_overlay.py --check")
    commit = sync.index("git commit -a -m fix")
    publish = sync.index("./scripts/publish_overlay.sh")
    assert check < commit < publish
    assert "scripts/bump_desktop_overlay.py\n" in sync
