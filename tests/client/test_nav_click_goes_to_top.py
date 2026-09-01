"""CLICKING A TIMELINE TAB TAKES YOU TO THE TOP OF IT.

Reported as: "when I click on social, it takes me half way down the feed, I have to scroll all the
way to the top manually on desktop classic. Can we fix that to take me to top as soon as I click on
the social tab?"

Half the behaviour was already there and that is what made it confusing. `activateNavView` had:

    if(target === VIEW && _TL_TABS.indexOf(target) >= 0){ timelineTop(target); return; }

— so tapping the tab you were ALREADY on went to the top, while arriving from any other screen fell
through to `requestView`, which restores `_tlScrollMemo`. The one gesture people use to check what
is new put them back in the middle of what they had already read.

The memo is not wrong, it is just not what a nav click means. Back, a thread opened and closed, a
window moved between monitors — those restore through history, and they still do.

Cleared rather than routed through `timelineTop`, because `requestView` carries a guard that must
not be lost: during login/startup boot is still awaiting config and must not replace the screen the
person just chose with its default landing.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _activate() -> str:
    start = APP_JS.index("  function activateNavView(v){")
    return APP_JS[start:APP_JS.index("  function timelineTop(view){", start)]


def test_arriving_from_another_view_lands_at_the_top():
    """THE REPORT. The branch must cover a timeline tab whether or not it is already open."""
    body = _activate()
    assert "if(_TL_TABS.indexOf(target) >= 0){" in body, (
        "the top-of-feed behaviour is gated on already being on the tab again")
    assert "delete _tlScrollMemo[target]" in body, "the remembered offset is still restored"
    assert "_tlForceTop = target" in body, (
        "nothing tells renderTimeline to ignore a restore that is already in flight")


def test_tapping_the_tab_you_are_on_still_refreshes():
    """That gesture is a REFRESH, not merely a scroll — it revives a stalled relay and redraws."""
    body = _activate()
    assert "if(target === VIEW){ timelineTop(target); return; }" in body
    top = APP_JS[APP_JS.index("  function timelineTop(view){"):]
    top = top[:top.index("\n  }") + 4]
    assert "Relay.reviveStale()" in top, "the refresh half of the gesture is gone"


def test_the_boot_guard_is_not_bypassed():
    """`requestView` must still be the thing that navigates: during login/startup boot is awaiting
    config and must not replace the screen the person just chose."""
    body = _activate()
    assert body.rstrip().endswith("requestView(target);\n  }") or "requestView(target);" in body
    assert body.index("_tlForceTop = target") < body.index("requestView(target)"), (
        "the force-top flag is set after navigation, so the render has already restored")


def test_a_non_timeline_view_is_untouched():
    """Notes, Files, Settings and the rest keep their own scroll behaviour — the memo is only
    cleared for the tabs this is about."""
    body = _activate()
    # The SECOND `_TL_TABS` branch — the first is the hidden-tab fallback, which is a different
    # decision entirely and grabbing it made this assertion measure nothing.
    anchor = body.index("if(target === VIEW){ timelineTop(target); return; }")
    block = body[body.rindex("if(_TL_TABS.indexOf(target) >= 0){", 0, anchor):]
    block = block[:block.index("\n    }") + 6]
    assert "delete _tlScrollMemo[target]" in block, "the clear escaped the timeline-only branch"


def test_history_restores_are_left_alone():
    """Back, a closed thread and a monitor handoff all restore through `_restoreNavScroll`, which
    this must not have touched — otherwise Back stops working to fix a nav click."""
    assert "function _restoreNavScroll(st){" in APP_JS
    fn = APP_JS[APP_JS.index("function _restoreNavScroll(st){"):]
    fn = fn[:fn.index("\n  }") + 4]
    assert "_putScroll(st.top" in fn
