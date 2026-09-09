"""COPY IMAGE HAD NEVER WORKED ON THE DESKTOP, AND THEN TOLD A MOUSE USER TO LONG-PRESS.

Reported from the PosterChanOS desktop (Electron, Wayland, the bundle served over `app://`): the
lightbox's Copy image button answered `copy failed — long-press the image to copy`. Both halves were
wrong. `navigator.clipboard.write` is refused on that origin exactly as `writeText` is -- which is
why `copyValue()` exists for text -- and `pcClip` was write-only TEXT, so there had never been a path
that could succeed. And long-press is not a gesture a desk with a mouse has.

Worse, every distinct cause -- no clipboard in this shell, a 403 on the image, a refused permission,
a conversion failure -- landed in ONE catch with that ONE line, so nothing on screen or in any log
distinguished "this app cannot do that here" from "that image is gone".

The shipped `_lbCopyImg` is RUN here against a stub of each shell, because a source-text assertion
would pass against a message wired to the wrong branch.

Run: venv-unified/bin/python -m pytest -p no:cacheprovider tests/client/test_lightbox_image_copy.py
"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The PNG signature the sim feeds in; the bridge must receive these bytes and not a re-encoding.
PNG = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 1, 2, 3]


@pytest.fixture(scope="module")
def result():
    out = subprocess.run(["node", str(ROOT / "tests/client/lightbox_copy_image_sim.mjs")],
                         cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_desktop_shell_copies_through_the_native_bridge(result):
    assert result["desktopToast"] == "image copied"
    assert result["desktopBytes"] == PNG, "the bridge was handed the wrong bytes"


def test_the_native_bridge_beats_a_present_web_clipboard(result):
    """Electron's own clipboard never takes the Wayland selection, so a fallback to it would copy
    into PosterChan and paste nothing into Firefox or Telegram -- the exact split the text path
    already exists to avoid."""
    assert result["bridgePreferred"] is True
    assert result["bridgePreferredToast"] == "image copied"


def test_no_message_tells_a_mouse_user_to_long_press(result):
    for key in ("refusedToast", "desktopFetchToast", "mouseUnavailable",
                "webFetchToast", "webRefusedToast"):
        assert "long-press" not in result[key], f"{key}: {result[key]!r}"


def test_a_touch_surface_is_the_only_one_offered_a_gesture(result):
    assert "long-press" in result["touchUnavailable"], result["touchUnavailable"]


def test_every_failure_names_an_action_that_exists_on_this_toolbar(result):
    for key in ("refusedToast", "desktopFetchToast", "mouseUnavailable", "touchUnavailable",
                "webFetchToast", "webRefusedToast"):
        assert "Save (⤓)" in result[key], f"{key}: {result[key]!r}"


def test_a_missing_clipboard_is_reported_as_missing_not_as_a_refusal(result):
    assert "can’t put images on the clipboard" in result["mouseUnavailable"]
    assert "refused" not in result["mouseUnavailable"]


def test_the_http_status_survives_into_the_message(result):
    assert "403" in result["desktopFetchToast"], result["desktopFetchToast"]
    assert "404" in result["webFetchToast"], result["webFetchToast"]
    assert "refused" not in result["webFetchToast"], "a dead image read as a refused clipboard"


def test_a_refused_clipboard_is_still_reported_as_refused(result):
    assert "refused the image" in result["webRefusedToast"]
    assert "load that image" not in result["webRefusedToast"]


def test_the_browser_path_still_writes_synchronously_with_the_tap(result):
    """iOS/Safari revoke the clipboard permission if you await before calling write(), so the
    ClipboardItem must still be handed a PENDING promise rather than resolved bytes."""
    assert result["webToast"] == "image copied"
    assert result["webHandedAPromise"] is True
    assert result["webSynchronous"] is True
