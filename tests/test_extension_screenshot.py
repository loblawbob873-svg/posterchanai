"""Saving a page as a picture — the parts that are dangerous when they are wrong.

Run: venv-unified/bin/python -m pytest tests/test_extension_screenshot.py

This runs with the user's signing key and photographs whatever is on screen, so what is asserted here
is not that a screenshot works; it is that it photographs THE RIGHT TAB, encrypts the way the app can
read, puts the bytes where the app will look for them, and puts the page back afterwards.

The worst one, and the reason this file exists: `captureVisibleTab` photographs whatever is ACTIVE in
the window — there is no API to capture a named tab. The popup closes the instant you click the tab
strip while the sweep keeps running for another twenty seconds, so without a check every remaining
tile is a picture of whatever you switched to — your bank, your webmail — stitched into a note titled
with the original page and kept in the encrypted drive with `X-Keep`, which exempts it from every
retention sweep. Forever.

These are source assertions because the alternative is a headless browser with an extension, two tabs
and a signing key to drive the real thing; what regresses silently here is the SHAPE.
"""
import re
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "extension"


@pytest.fixture(scope="module")
def shot():
    return (EXT / "shot.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def drive():
    return (EXT / "drive.js").read_text(encoding="utf-8")


def test_it_refuses_to_photograph_a_tab_you_switched_away_from(shot):
    assert "tabs.get(tabId)" in shot, "the sweep never re-checks which tab it is photographing"
    assert re.search(r"if\s*\(!tab \|\| !tab\.active\)", shot), (
        "nothing stops the sweep when the target tab stops being the active one — every remaining "
        "tile would be a picture of whatever the user switched to")


def test_it_captures_by_window_not_by_guessing(shot):
    assert "captureVisibleTab(tab.windowId" in shot, (
        "captureVisibleTab(null) means 'the active tab of the current window', which is not "
        "necessarily the window being scrolled")


def test_a_scroll_position_of_zero_is_a_real_answer(shot):
    """`(at || y)` treats 0 as missing, which defeats the stall guard: a page held at the top by a
    consent modal then stitches sixty copies of the same screen."""
    assert "?? y" in shot, "the scroll read uses a falsy check again"
    assert not re.search(r"\(at \|\| y\)", shot)


def test_the_sweep_stops_when_the_page_stops_moving(shot):
    assert "at <= last" in shot, "nothing detects a page that refuses to scroll further"


def test_smooth_scrolling_is_turned_off_for_the_sweep(shot):
    """`html{scroll-behavior:smooth}` makes scrollTo animate, so a position read afterwards is a tile
    behind — which drew tiles over each other and ended the sweep two screenfuls in, presenting that
    as the whole page."""
    assert "scrollBehavior = 'auto'" in shot
    assert "__pcShotBehav" in shot, "the page's own scroll-behavior is not restored"


def test_the_position_is_read_in_its_own_step(shot):
    """Reading scrollY in the same call that asked for the scroll returns where the browser had got
    to at that instant, not where it ends up."""
    assert "step === 'at'" in shot
    assert "if (step === 'to') { window.scrollTo(0, y); return true; }" in shot


def test_the_page_is_restored_even_when_measuring_throws(shot):
    """Measuring is not read-only — it hides every fixed and sticky element and forces the scroll
    style — so a throw outside the finally leaves the page with an invisible header."""
    i, j = shot.index("say('measuring…')"), shot.index("finally {")
    body = shot[i:j]
    assert body.index("try {") < body.index("'measure'"), (
        "the restore is armed after the page has already been mutated")


def test_the_real_device_pixel_ratio_is_used(shot):
    """A canvas sized to a clamped ratio and a tile drawn at its natural size disagree by that factor:
    on a 3x display every tile is 50% oversized and the right third falls off the canvas."""
    assert "Math.min(m.dpr" not in shot, "the devicePixelRatio is being clamped again"
    draw = next(l for l in shot.split("\n") if "cx.drawImage(" in l)
    assert "Math.round(m.w * dpr)" in draw and "Math.round(m.vh * dpr)" in draw, (
        "drawImage does not pass a destination size, so the tile is drawn at whatever scale it "
        f"arrives: {draw.strip()}")


def test_the_blob_goes_where_the_app_will_look_for_it(drive):
    """The app reads `pcres:` attachments from mediaServer(), which is the user's OWN Blossom server
    when they have configured one. Uploading to the instance instead writes a note whose picture 404s
    on the only screen it is ever opened from."""
    assert "function blobBase(" in drive
    assert "cfg.media" in drive, "the pairing's media-server address is ignored"
    assert "cfg.api + '/blossom/upload'" not in drive, "the upload is hardcoded to the instance again"


def test_the_envelope_matches_the_app(drive):
    """12-byte IV prepended, AES-GCM — byte-for-byte what _masterEncrypt writes, because the app is
    what has to read it back."""
    assert "new Uint8Array(12)" in drive
    assert "out.set(iv, 0)" in drive and "out.set(ct, iv.length)" in drive


def test_it_seals_with_the_drive_key_not_the_vault_key(drive):
    """Two different keys. The vault key seals passwords and notes; drive blobs use the account master
    key. Sealing with the wrong one produces a blob the app cannot open — which is worse than failing,
    because it looks like it worked."""
    assert "files-index" in drive and "getConversationKey" in drive
    assert "masterKey" in drive


def test_the_hash_is_ours_not_the_servers(drive):
    """The note points at the blob by hash; taking that from the response would let a wrong answer
    produce a permanently unreadable note."""
    i = drive.index("async function upload(")
    body = drive[i:]
    assert "_sha256Hex(sealed)" in body
    assert re.search(r"return \{ sha,", body), "the returned sha is not the one we computed"


def test_only_the_popup_can_ask_for_a_capture():
    """A page able to ask for this would be a page able to photograph whatever tab you were looking
    at, and to write a note with your key."""
    bg = (EXT / "background.js").read_text(encoding="utf-8")
    i = bg.index("case 'page-save'")
    assert "_fromPopup(sender)" in bg[i:i + 400], "page-save is reachable from a content script"


def test_two_sweeps_cannot_run_at_once():
    """The restore state is per-page and shared, so overlapping sweeps restore the wrong set and leave
    a page's header invisible."""
    bg = (EXT / "background.js").read_text(encoding="utf-8")
    assert "_shotBusy" in bg


def test_both_new_files_ship_in_both_builds():
    """A file that is not in FILES is not in the .zip, and an add-on that loads without it is an
    add-on whose button throws."""
    build = (EXT / "build.sh").read_text(encoding="utf-8")
    worker = (EXT / "background-chrome.js").read_text(encoding="utf-8")
    import json
    man = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    for f in ("shot.js", "drive.js"):
        assert f in build, f"{f} is not in build.sh's FILES, so it ships in neither zip"
        assert f in worker, f"{f} is not imported by the Chrome service worker"
        assert f in man["background"]["scripts"], f"{f} is not in the Firefox background scripts"
