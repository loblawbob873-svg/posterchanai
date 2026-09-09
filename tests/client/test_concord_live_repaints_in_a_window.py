"""A live message must PAINT, not just be saved, when Concord is in a desktop window.

Reported as "new concord room messages are only appearing after I send or switch rooms", on the
PosterChanOS desktop. Both live-flush paths gated their repaint on
`document.body.classList.contains('concord-view')` — which is set only when Concord is the active
FULL-PAGE view. In the windowed desktop it lives in an OS window, so the arriving message was
decrypted, merged and saved, and then never painted. Sending a message and switching rooms both
repaint, which is why they looked like the cure and why the report reads as a stale view.

`refreshActiveChannel` already asked the right question (foreground OR parked). The render gate did
not. These run the shipped predicate.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def _run(body_class, pcos):
    fn = SRC[SRC.index("  function chatOnScreen(){"):]
    fn = fn[:fn.index("\n  async function flushChatLive(")]
    script = f"""
globalThis.document={{body:{{classList:{{contains:c=>{body_class!r}.includes(c)}}}}}};
globalThis.window={pcos};
{fn}
console.log(chatOnScreen()?'PAINT':'SKIP');
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


ON = "{PCOS:{isOn:()=>true,parkedSlot:s=>s==='concord'}}"
OFF = "{}"


def test_the_full_page_view_paints():
    assert _run(["concord-view"], OFF) == "PAINT"


def test_a_parked_desktop_window_paints():
    """The reported case: Concord in an OS window, body without the full-page class."""
    assert _run([], ON) == "PAINT"


def test_neither_does_not_paint():
    """Concord genuinely not on screen: save without painting, as before."""
    assert _run([], OFF) == "SKIP"


def test_a_desktop_with_no_concord_window_does_not_paint():
    assert _run([], "{PCOS:{isOn:()=>true,parkedSlot:()=>null}}") == "SKIP"


def test_a_broken_shell_bridge_cannot_break_delivery():
    """PCOS throwing must not take the message path down with it."""
    assert _run([], "{PCOS:{isOn(){throw new Error('boom');}}}") == "SKIP"
    assert _run(["concord-view"], "{PCOS:{isOn(){throw new Error('boom');}}}") == "PAINT"


def test_both_live_paths_use_it():
    """The NIP-29 and Cord flushes are two paths to one screen; a fix to one is half a fix."""
    flush = SRC[SRC.index("  async function flushChatLive("):SRC.index("  function mergeCordTimeline(")]
    absorb = SRC[SRC.index("  async function absorbChatWraps("):SRC.index("  async function refreshActiveChannel(")]
    assert "chatOnScreen()" in flush, "the NIP-29 live path still gates on the full-page class"
    assert "chatOnScreen()" in absorb, "the Cord live path still gates on the full-page class"
    assert "document.body.classList.contains('concord-view')" not in absorb
