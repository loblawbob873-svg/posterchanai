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
(`routeView`'s `doc:os-settings` → 'settings').

THE RULE HAS SINCE MOVED, AND IT MOVED BECAUSE REFUSING WAS ITSELF A BUG. The answer used to be
that these frames cannot be popped out at all — Settings, Task Manager, VMs and Remote Desktop are
EXTRAS this shell BUILDS, with no app view a fresh page could render. True at the time, and it left
them as IN-PAGE frames, which on PosterChanOS is the other half of the same failure: showing an
in-page frame means raising the desktop's own full-output surface, so opening System Settings hid
every native window on the screen ("why the fuck are windows still hiding when I open a app like
system settings"). No stacking order fixes that; only being a real toplevel does.

So the rule is no longer "never" — it is CAN A FRESH PAGE DRAW THIS. An extra pops out exactly when
os.js has a renderer for it (`EXTRA_RENDER`) and oswin.js will route it (`EXTRA_VIEWS`); one with no
renderer, and a folder, still refuse. Both sets are READ FROM THE SHIPPED CODE below rather than
typed here — a renderer added on one side and forgotten on the other is precisely the shape that
produces a correctly-titled window painting somebody else's screen.

Two rules, at two levels, because one of them has to survive the next caller:

  * `popOutView` decides what the BUTTON offers — a view the nav actually knows, which is the same
    list the desktop reads to draw its icons, so a view added to the nav is poppable for free;
  * `PCOSWin.open` REFUSES an unroutable view outright, stated where no future caller can get round
    it, since the failure mode is a success with the wrong contents rather than an error.

These run both shipped rules against a stub nav, because the question is what the code decides.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(__file__).with_name("oswin_popout_runtime.mjs")
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
OSWIN_JS = (ROOT / "static/js/client/oswin.js").read_text(encoding="utf-8")
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


def _extra_windows():
    """os.js's map of in-page frame identity -> the view a fresh page is asked to render."""
    block = OS_JS.split("const EXTRA_WINDOWS = {", 1)[1].split("\n  };", 1)[0]
    return dict(re.findall(r"'([^']+)':\s*\{\s*view:\s*'([^']+)'", block))


def _extra_renderers():
    """The extras os.js can actually DRAW in a page with no desktop behind it."""
    block = OS_JS.split("const EXTRA_RENDER = {", 1)[1].split("\n  };", 1)[0]
    return set(re.findall(r"'(__[a-z]+)'\s*:", block))


def _routable_extras():
    """The extras oswin.js will route in a window, rather than handing to switchView."""
    line = OSWIN_JS.split("const EXTRA_VIEWS = [", 1)[1].split("]", 1)[0]
    return set(re.findall(r"'(__[a-z]+)'", line))


def test_the_two_halves_of_the_rule_agree():
    """A renderer oswin will not route is a window that paints the timeline; a routable name with no
    renderer is an empty window. Both are the reported bug wearing different clothes, and each side
    is edited in a different file — so assert them against each other rather than against a list."""
    assert _extra_renderers() == _routable_extras(), (
        "os.js EXTRA_RENDER and oswin.js EXTRA_VIEWS disagree about which extras a window can show")
    for frame, target in _extra_windows().items():
        assert target in _extra_renderers(), (
            f"{frame} is offered a window as {target}, which nothing can draw")


def test_system_settings_opens_on_a_view_a_window_can_actually_draw():
    """THE BUG, by name. `doc:os-settings` is a frame this shell builds, and the old code turned it
    into `os-settings` — a name nothing routes, which lands on the feed. It may open now, but only
    ever on the name its renderer answers to."""
    got = pop_view({"view": "doc:os-settings", "label": "System Settings"})
    assert got != "os-settings", (
        "the doc: prefix is being stripped again — this is the social feed under the Settings title")
    assert got == "__ossettings"
    assert got in _extra_renderers() and got in _routable_extras()


@pytest.mark.parametrize("view", ["__ossettings", "__tasks", "__vms", "__remote", "__bug"])
def test_an_extra_pops_out_only_where_a_window_could_draw_it(view):
    """Named individually so a new EXTRA cannot quietly inherit either failure: popping out with
    nothing to render, or staying in-page and hiding every native window behind the shell."""
    got = pop_view({"view": view})
    target = _extra_windows().get(view, "")
    if target and target in _extra_renderers():
        assert got == target, f"{view} can be drawn in a window but is still refused one"
        assert got in _routable_extras()
    else:
        assert got == "", (
            f"{view} offers to open as a real window and nothing can paint it")


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
