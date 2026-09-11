from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_overlay_audits_both_places_a_conversation_can_live():
    """THE PACKAGE MUST CARRY BOTH, and this test used to insist on the wrong two.

    It pinned the UNIFIED surface — `messages-communities` / `messages-direct`, the tabs of a
    Messages screen that also held communities — and went further, asserting a Concord navigation
    entry was NOT audited. When Communities became its own place those markers stopped existing, so
    the publisher refused every desktop build after the split with "missing required runtime
    surfaces", the overlay silently stopped advancing, and this test was GUARDING the breakage: it
    would have failed on the fix.

    The rule it was always reaching for survives: a stale asar must not be able to ship one of the
    two without the other. It is expressed against the navigation entries that exist now.
    """
    source = (ROOT / "scripts" / "bump_desktop_overlay.py").read_text(encoding="utf-8")

    assert "index.html Messages navigation entry" in source
    assert "index.html Communities navigation entry" in source
    assert "b'data-view=\"messages\"'" in source
    assert "b'data-view=\"concord\"'" in source
    # The retired markers must not come back AS AUDITS — they name a surface the client no longer
    # renders, so checking for them can only ever refuse a good build. Naming them in a COMMENT is
    # fine and in fact wanted: that comment is the record of why the publisher once blocked.
    assert "b'messages-communities'" not in source
    assert "b'messages-direct'" not in source


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
