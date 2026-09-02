"""A WINDOW MUST NOT OPEN ON SOMETHING IT CANNOT SHOW.

Reported as "Systems settings just loaded a social feed" — and that is precisely what it did.

The ⧈ "Open as a real window" button derived the new window's view by stripping `doc:`:

    const view = String((w.appView || w.view || '').replace(/^doc:/, ''));

System Settings is an in-page frame whose view is `doc:os-settings`, so it asked for `os-settings`.
Nothing routes that name, and `switchView` does NOT validate its argument — an unknown view is set
and falls through to the default timeline. So the window opened successfully, took the title
"System Settings", and painted the social feed. Nothing threw, nothing logged, and every other
window popped out correctly, which is why it read as random.

Stripping the prefix was the wrong repair to begin with: os.js already carries the real mapping
(`routeView`'s `doc:os-settings` → 'settings'). But the honest answer for these frames is that they
cannot be popped out at all — Settings, Task Manager, VMs and Remote Desktop are EXTRAS this shell
BUILDS into a frame, with no app view a fresh page could render, and a folder is not an app either.

Two rules, at two levels, because one of them has to survive the next caller:

  * `popOutView` decides what the BUTTON offers — a view the nav actually knows, which is the same
    list the desktop reads to draw its icons, so a view added to the nav is poppable for free;
  * `PCOSWin.open` REFUSES an unroutable view outright, stated where no future caller can get round
    it, since the failure mode is a success with the wrong contents rather than an error.

These run both shipped rules against a stub nav, because the question is what the code decides.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(__file__).with_name("oswin_popout_runtime.mjs")
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE, reason="node unavailable")


def _node(script: str):
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def pop_view(window, nav=None):
    """What the shipped popOutView rule answers for this window."""
    return _node(f"""
        import {{ popOutView }} from './tests/client/oswin_popout_runtime.mjs';
        console.log(JSON.stringify(popOutView({json.dumps(window)},
                    {json.dumps(nav or ['home', 'global', 'settings', 'mail'])})));
    """)


def opened_for(view, nav=None):
    """Whether PCOSWin.open actually opens a window for this view, and on what URL."""
    return _node(f"""
        import {{ oswin }} from './tests/client/oswin_popout_runtime.mjs';
        const w = oswin({{ nav: {json.dumps(nav or ['home', 'global', 'settings', 'mail'])} }});
        const child = w.api.open({json.dumps(view)}, 'label');
        console.log(JSON.stringify({{ opened: !!child, urls: w.opened.map(o => o.url) }}));
    """)


def test_system_settings_does_not_offer_a_pop_out(pop=None):
    """THE BUG, by name. `doc:os-settings` is a frame this shell builds, not a view a fresh page
    can render — and the old code turned it into `os-settings`, which lands on the feed."""
    assert pop_view({"view": "doc:os-settings", "label": "System Settings"}) == "", (
        "System Settings still offers to open as a real window, and there is nothing for that "
        "window to render — this is the social feed under the Settings title")


@pytest.mark.parametrize("view", ["__ossettings", "__tasks", "__vms", "__remote", "__bug"])
def test_no_extras_window_offers_a_pop_out(view):
    """Every EXTRA has the same shape as the one that was reported. Naming them individually so a
    new one cannot quietly inherit the bug."""
    assert pop_view({"view": view}) == ""


def test_a_folder_is_not_an_application():
    assert pop_view({"view": "folder:games"}) == ""


def test_a_real_app_window_still_pops_out():
    """The feature has to survive its own fix — the point of the button is that a real window is
    stacked by sway instead of faked inside the shell."""
    assert pop_view({"view": "home"}) == "home"
    assert pop_view({"view": "mail", "appView": "mail"}) == "mail"


def test_a_view_the_nav_does_not_know_is_refused():
    """The general rule rather than a list: the nav IS the list of apps this client can render, and
    it is what the desktop already reads to draw its icons."""
    assert pop_view({"view": "nosuchview"}) == ""
    assert pop_view({"view": "torrents"}, nav=["home"]) == "", (
        "a view hidden from this instance's nav is still offered a window it cannot fill")


def test_the_window_api_refuses_an_unroutable_view_on_its_own():
    """Defence at the level that survives the next caller. The reported failure was a SUCCESS with
    the wrong contents, so the check cannot live only in the one place that got it wrong."""
    got = opened_for("os-settings")
    assert got["opened"] is False, "PCOSWin.open still opens a window on a view nothing routes"
    assert got["urls"] == [], "it opened the window before deciding it should not have"


def test_the_window_api_still_opens_a_real_view():
    got = opened_for("home")
    assert got["opened"] is True
    assert got["urls"] == ["/client?pcwin=home"]


def test_the_button_is_not_drawn_for_a_window_that_cannot_be_popped_out():
    """A control that declines when pressed is better than one that lies, but a control that is not
    there is better still — and this is the one the user would otherwise press again."""
    guard = OS_JS.split("""{ const pop = $('.osw-b[data-w="pop"]', el);""", 1)[1].split("}", 1)[0]
    assert "popOutView(w)" in guard, (
        "the pop-out button is offered on every window again, including the ones with no view to "
        "open")


def test_popout_rechecks_at_click_time():
    """`appView` changes while a window is open (the Messages tabs do exactly that), so the check
    made when the frame was drawn cannot be the only one."""
    body = OS_JS.split("  function popOut(w){", 1)[1].split("\n  }", 1)[0]
    assert "popOutView(w)" in body, "popOut no longer resolves the view through the shared rule"
    assert "replace(/^doc:/" not in body, (
        "popOut is back to stripping the doc: prefix, which is what produced `os-settings`")
