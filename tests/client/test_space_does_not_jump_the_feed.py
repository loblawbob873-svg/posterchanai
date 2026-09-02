"""SPACE MUST NOT THROW YOUR PLACE IN THE TIMELINE AWAY — and must still type and still press.

Reported as "space bar sometimes skips down the social page". The "sometimes" is the whole clue:
`.feed` is the scroll container (`overflow-y:auto`) and is not focusable, so a space scrolls
whatever scrollable ancestor the FOCUSED element has. Click a post — a plain div — and focus is
inside the feed, so space pages it down. Click nothing, focus is on body, whose document does not
scroll, and space does nothing. One key, two behaviours, decided by whatever you last touched.

Two exceptions are not negotiable, and suppressing either would be a worse bug than the one being
fixed: space TYPES in a field, and space ACTIVATES a focused control — that is how a keyboard
presses a button. This runs the shipped handler against a stub DOM for each case.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _handler() -> str:
    i = APP.index("  function _bindSpaceDoesNotJump(){")
    depth, start = 0, APP.index("{", i)
    for j in range(start, len(APP)):
        if APP[j] == "{":
            depth += 1
        elif APP[j] == "}":
            depth -= 1
            if depth == 0:
                return APP[i:j + 1]
    raise AssertionError("could not lift the handler")


def press(target: dict, key=" ", ctrl=False) -> bool:
    """True when the default page-scroll was prevented."""
    program = """
      let handler = null;
      global.document = { addEventListener: (_t, fn) => { handler = fn; } };
      %(fn)s
      _bindSpaceDoesNotJump();
      const spec = %(target)s;
      const target = {
        tagName: spec.tag || 'DIV',
        isContentEditable: !!spec.editable,
        closest: (sel) => {
          // the stub answers for the ancestors this element declares
          for(const a of (spec.ancestors || [])) {
            for(const one of sel.split(',').map(s => s.trim())) {
              if(one === a) return { tag: a };
            }
          }
          return null;
        },
      };
      let prevented = false;
      handler({ key: %(key)s, ctrlKey: %(ctrl)s, metaKey: false, altKey: false,
                target, preventDefault: () => { prevented = true; } });
      process.stdout.write(JSON.stringify(prevented));
    """ % {"fn": _handler(), "target": json.dumps(target), "key": json.dumps(key),
           "ctrl": "true" if ctrl else "false"}
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout)


IN_FEED = {"tag": "DIV", "ancestors": [".feed"]}


def test_space_on_a_post_does_not_page_the_feed():
    """THE BUG: a post card is a plain div, and focus lands on it when you click."""
    assert press(IN_FEED) is True


def test_space_still_types_in_a_text_field():
    for tag in ("INPUT", "TEXTAREA"):
        assert press({"tag": tag, "ancestors": [".feed"]}) is False, f"space stopped typing in {tag}"


def test_space_still_types_in_a_contenteditable():
    assert press({"tag": "DIV", "editable": True, "ancestors": [".feed"]}) is False


@pytest.mark.parametrize("ancestor", ["button", "a[href]", "summary", "label",
                                      '[role="button"]', '[role="checkbox"]', '[role="menuitem"]'])
def test_space_still_presses_a_control(ancestor):
    """Space is how a keyboard activates a button. Breaking that to fix a scroll would be a far
    worse bug, and an accessibility regression."""
    assert press({"tag": "DIV", "ancestors": [ancestor, ".feed"]}) is False, (
        f"space no longer activates a focused {ancestor}")


def test_outside_the_feed_the_browser_default_is_left_alone():
    """Elsewhere a page-scroll is harmless and somebody may rely on it. Only the timeline loses
    something it cannot get back."""
    assert press({"tag": "DIV", "ancestors": []}) is False


def test_modified_space_is_not_touched():
    assert press(IN_FEED, ctrl=True) is False


def test_other_keys_are_not_touched():
    for key in ("a", "PageDown", "ArrowDown", "Enter"):
        assert press(IN_FEED, key=key) is False, f"{key} is being swallowed"


def test_the_legacy_spacebar_key_name_is_handled():
    """Older engines report ' ' as 'Spacebar'."""
    assert press(IN_FEED, key="Spacebar") is True


def test_it_is_actually_wired_up():
    """A handler nothing calls changes nothing — the failure this codebase has paid for repeatedly."""
    assert "_bindSpaceDoesNotJump();" in APP.split("function bindFeedActions(){", 1)[1][:300]
