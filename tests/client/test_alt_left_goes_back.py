"""THERE MUST BE A WAY BACK FROM ANYWHERE, INCLUDING A WINDOW WITH NO CHROME.

Reported three times in a row, each time reading like a different bug:

  * "i just opened a profile from social and now can't go back to social"
  * "i added contact in texts and no way to go back to texts window, I am stuck in Contacts now"
  * "clicking on Messages, opening a convo, click on avatar, you go to a profile page, problem is
    no way back to messages! clicking on desktop icon for messages brings up the profile window!"

One cause. A desktop WINDOW has no sidebar and no browser chrome, and the screens that navigate
inside it — a profile, a thread, Contacts — set the client's VIEW directly instead of going through
switchView, so the window still calls itself Messages while showing a profile. Pressing the app's
icon focuses that window and hands the profile back.

The app has a real history and Android's back button already walks it; nothing on a desktop did.
Alt+Left is what every browser and file manager uses, so it needs no explaining, and it works the
same on the web, in the shell and inside a popped-out window.

NOT a complete answer to the third report: making the ICON return a popped-out window to its own
view needs the shell to tell that window, and a window is a separate renderer. This gives a way out
from inside it, which is what was missing entirely.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


def _handler() -> str:
    start = APP.index("  function _bindAltLeftGoesBack(){")
    depth, i = 0, APP.index("{", start)
    for j in range(i, len(APP)):
        if APP[j] == "{":
            depth += 1
        elif APP[j] == "}":
            depth -= 1
            if depth == 0:
                return APP[start:j + 1]
    raise AssertionError("handler")


def press(key="ArrowLeft", alt=True, ctrl=False, shift=False, tag="DIV",
          editable=False, pushed=1) -> dict:
    program = """
      let handler = null;
      global.document = { addEventListener: (_t, fn) => { handler = fn; } };
      let _navPushed = %(pushed)d;
      let went = false;
      global.history = { back: () => { went = true; } };
      %(fn)s
      _bindAltLeftGoesBack();
      let prevented = false;
      handler({ key:%(key)s, altKey:%(alt)s, ctrlKey:%(ctrl)s, metaKey:false, shiftKey:%(shift)s,
                target:{ tagName:%(tag)s, isContentEditable:%(editable)s },
                preventDefault: () => { prevented = true; } });
      process.stdout.write(JSON.stringify({went, prevented}));
    """ % {"fn": _handler(), "pushed": pushed, "key": json.dumps(key),
           "alt": "true" if alt else "false", "ctrl": "true" if ctrl else "false",
           "shift": "true" if shift else "false", "tag": json.dumps(tag),
           "editable": "true" if editable else "false"}
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout)


def test_alt_left_goes_back():
    """THE FIX. The only way out of a navigated window that has no chrome."""
    got = press()
    assert got["went"] is True and got["prevented"] is True


def test_it_does_nothing_with_no_history_of_our_own():
    """A cold deep link pushed no entry; going back would leave the app entirely."""
    assert press(pushed=0)["went"] is False


@pytest.mark.parametrize("tag", ["INPUT", "TEXTAREA", "SELECT"])
def test_it_does_not_fire_while_typing(tag):
    """Alt+Left moves the caret by a word in a text field on some platforms; stealing it would be a
    worse bug than the one being fixed."""
    assert press(tag=tag)["went"] is False


def test_it_does_not_fire_in_a_contenteditable():
    assert press(editable=True)["went"] is False


@pytest.mark.parametrize("mods", [{"alt": False}, {"ctrl": True}, {"shift": True}])
def test_only_plain_alt_left(mods):
    assert press(**mods)["went"] is False


def test_other_keys_are_untouched():
    for key in ("ArrowRight", "a", "Backspace"):
        assert press(key=key)["went"] is False, f"{key} is being treated as Back"


def test_it_is_wired_up():
    """A handler nothing calls is the failure this codebase keeps paying for."""
    assert "_bindAltLeftGoesBack();" in APP.split("function bindFeedActions(){", 1)[1][:400]
