from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_overlay_audits_unified_messages_surface():
    source = (ROOT / "scripts" / "bump_desktop_overlay.py").read_text(encoding="utf-8")

    assert "index.html Messages navigation entry" in source
    assert "unified Messages direct/community tabs" in source
    assert "b'messages-communities'" in source
    assert "b'messages-direct'" in source
    assert "index.html Concord navigation entry" not in source


def test_overlay_refuses_a_mode_stripped_first_run_tor_binary():
    source = (ROOT / "scripts" / "bump_desktop_overlay.py").read_text(encoding="utf-8")
    assert '"*/resources/tor/tor/tor"' in source
    assert "stat.S_IXUSR" in source
    assert "first-run would fail EACCES" in source


def test_version_bump_renames_the_existing_ebuild_instead_of_recreating_it():
    """The /usr/local wrapper and Tor fperms fixes must survive every immutable version bump."""
    source = (ROOT / "scripts" / "bump_desktop_overlay.py").read_text(encoding="utf-8")
    assert 'subprocess.run(["git", "mv"' in source
    assert "open(os.path.join(PKG, new_file), \"w\"" not in source
