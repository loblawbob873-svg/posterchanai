from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_overlay_audits_unified_messages_surface():
    source = (ROOT / "scripts" / "bump_desktop_overlay.py").read_text(encoding="utf-8")

    assert "index.html Messages navigation entry" in source
    assert "unified Messages direct/community tabs" in source
    assert "b'messages-communities'" in source
    assert "b'messages-direct'" in source
    assert "index.html Concord navigation entry" not in source
