"""EVERY APP ON PosterChanOS IS A POPPED-OUT WINDOW, AND NOTHING EVER TESTED ONE.

"every time i use this POS OS, i find bugs stopping me from doing what I need to do" — and this is
why. `oswin.js` is ON BY DEFAULT on PosterChanOS, so opening ANY app from the desktop makes a real
toplevel, which the preload marks `backgroundOwner: false` (a window must never become a second
folder-sync writer with the same device identity). Every browser check in this suite runs in a plain
page where `window.pcShell` is undefined, so `backgroundOwner !== false` and every one of these
paths tests as working.

TWO BUGS SHIPPED THROUGH THAT GAP, both reported by using the machine:

  * Folder Sync — `FS()` returned null in a window document, so "Set up on this device…" rendered
    DISABLED. Clicking it did nothing, silently, in the only place anybody ever sees it.
  * System Settings — it had no way to be anything but an in-page frame, and showing one means
    raising the desktop surface, which is opaque and fills the output, so it HID every window it
    overlapped.

The rule this file pins is not about either screen: a control that is the POINT of a screen must be
usable in the window the product actually opens that screen in. It is written against the shipped
source rather than a live desktop because the failure is a decision made at render time from
`backgroundOwner`, which any DOM-free reading of the code can check — and because a check needing a
real Wayfire session runs on one machine and therefore never runs.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SYNC = (ROOT / "static/js/client/sync.js").read_text(encoding="utf-8")


def fs_body():
    """The FS() arrow function, to its closing `};` — bounded, because a fixed character slice runs
    past it into whatever helper comes next and asserts against the wrong code."""
    i = SYNC.index("const FS = () => {")
    return SYNC[i:SYNC.index("\n  };", i) + len("\n  };")]


def fs_deps():
    """Constants FS() closes over, emitted into the sandbox with it.

    FS walks the opener chain under a bound, and the bound is a `const` OUTSIDE the arrow function.
    Slicing the arrow alone left it undefined in the sandbox — where `hop < undefined` is false, so
    the loop never ran, FS returned null for EVERY shape, and the failure looked like a product bug
    rather than a missing dependency. A fixture must carry what the code it runs depends on, or it
    reports on something that does not exist."""
    m = re.search(r"const (FS_OPENER_HOPS)\s*=\s*(\d+);", SYNC)
    assert m, "FS's opener bound moved — re-point this fixture"
    return f"const {m.group(1)} = {m.group(2)};"
OSWIN = (ROOT / "static/js/client/oswin.js").read_text(encoding="utf-8")
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def test_a_window_document_is_never_the_background_owner():
    """The premise. If this stops being true the rest of the file is measuring nothing."""
    assert "const backgroundOwner = !_isWindowDoc" in PRELOAD


def test_folder_sync_does_not_simply_refuse_a_secondary_surface():
    """It used to `return null`, which disables the one button the screen exists for."""
    block = fs_body()
    assert "window.opener" in block, (
        "a popped-out Folder Sync window has no filesystem bridge again — its setup button will "
        "render disabled and click silently")


def test_it_borrows_the_primary_bridge_rather_than_making_its_own():
    """ONE writer per device is the rule that gate exists for, and it still holds: the child uses
    the OPENER's bridge, so the pick, the grant and the sweep all belong to the primary surface."""
    block = fs_body()
    # THE RULE, NOT THE VARIABLE. This pinned `owner.pcFs` from the single-hop version. The walk
    # follows the opener CHAIN now — on a second monitor the chain is window → secondary → primary,
    # and one hop found a secondary and gave up, disabling the button on every screen but one. What
    # has to hold is unchanged: the bridge comes from the PRIMARY surface and never from another
    # secondary, because that would be two writers again.
    assert ".pcFs" in block, "the child no longer borrows a bridge at all"
    assert "backgroundOwner === false" in block, (
        "nothing distinguishes a secondary surface any more, so the child could borrow from another "
        "one — two writers on one device")
    assert "opener" in block, "the child no longer looks to its opener for the primary"


def test_an_unreachable_opener_still_refuses():
    block = fs_body()
    assert ".closed" in block, "a closed window in the chain is followed as though it were live"
    assert block.rstrip().endswith("};")
    assert "return null;" in block, "with no opener there is nothing safe to hand back"


def test_the_setup_button_is_only_disabled_when_there_is_genuinely_no_bridge():
    """Not a rule about styling: `disabled` is how this bug presented, so the condition matters."""
    assert re.search(r"sync-attach[^\n]*\$\{fs \? '' : ' disabled'\}", SYNC), \
        "the attach button's enabled-ness no longer follows the bridge"


def test_system_settings_can_open_as_a_real_toplevel():
    """An in-page frame can only be shown by raising an opaque full-output surface over the
    applications. Being a real toplevel is the only thing that fixes that."""
    assert "EXTRA_WINDOWS" in OS
    assert "'doc:os-settings'" in OS and "__ossettings" in OS
    assert "__ossettings" in OSWIN, "oswin will refuse to open it"


def test_an_extra_is_drawn_by_the_desktop_not_by_switchView():
    """`switchView` does not validate its argument: an unknown view sets VIEW and falls through to
    the timeline, which is how "System settings just loaded a social feed" happened."""
    assert "PCOS.renderExtra" in OSWIN or "renderExtra" in OSWIN
    block = OSWIN[OSWIN.index("EXTRA_VIEWS.includes(v) &&"):][:400]
    assert "renderExtra" in block
    assert OSWIN.index("EXTRA_VIEWS.includes(v) &&") < OSWIN.index("__PC.switchView(v)"), \
        "switchView wins, so the window would paint the timeline under the right title"


def test_renderExtra_refuses_a_name_it_cannot_draw():
    block = OS[OS.index("function renderExtra(view)"):][:600]
    assert "if(!paint) return false;" in block, (
        "an unknown extra would fall through and the window would show whatever was already there")


def test_the_first_paint_of_a_new_window_also_routes_an_extra():
    """`routeFromPath` is what lands a freshly-opened window on its view, and it called switchView
    directly. Patching only the re-route channel would leave a NEW System Settings window showing
    the timeline — which is the whole bug, just moved to the path people actually take."""
    block = APP[APP.index("async function routeFromPath()"):][:1600]
    assert "PCOS.renderExtra(v)" in block, "a new window still lands via switchView only"
    assert block.index("PCOS.renderExtra(v)") < block.index("switchView(v)"), \
        "switchView runs first, so the window paints the timeline"


def test_the_extra_landing_falls_through_when_it_cannot_draw():
    """renderExtra answers false for a name it does not know; the ordinary landing must still
    happen, or an unrecognised window would show nothing at all."""
    block = APP[APP.index("async function routeFromPath()"):][:1600]
    assert "if(v){ switchView(v); return; }" in block


# ───────────────────────── the other three extras, which had the same defect ─────────────────────
#
# System Settings was only the one that got reported. Task Manager, Virtual Machines and Remote
# Desktop were built the same way — open a frame, paint into `w.slot` — so every one of them hid the
# windows behind it for exactly the same reason. The painters are separated from the window now, and
# what follows RUNS them with no window at all, because that is the claim.

import json
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
PAINTERS = {"__tasks": "paintTaskManager", "__vms": "paintVmManager",
            "__remote": "paintRemoteDesktop"}


def _extra_render_map():
    """os.js's map of window view -> renderer, read from the shipped source."""
    block = OS.split("const EXTRA_RENDER = {", 1)[1].split("\n  };", 1)[0]
    return dict(re.findall(r"'(__[a-z]+)'\s*:\s*\(\)\s*=>\s*([A-Za-z_]+)", block))


def test_every_extra_that_can_open_a_window_has_something_to_draw_it():
    """THE RULE THAT KEEPS THIS HONEST. A name in EXTRA_WINDOWS with no renderer opens an empty,
    correctly-titled window — which reads exactly like the bug it was meant to fix, and worse than
    the frame it replaced."""
    windows = dict(re.findall(r"'([^']+)':\s*\{\s*view:\s*'([^']+)'",
                              OS.split("const EXTRA_WINDOWS = {", 1)[1].split("\n  };", 1)[0]))
    renderers = _extra_render_map()
    for frame, target in windows.items():
        assert target in renderers, f"{frame} opens a window as {target} and nothing can paint it"
    routable = set(re.findall(r"'(__[a-z]+)'", OSWIN.split("const EXTRA_VIEWS = [", 1)[1].split("]", 1)[0]))
    assert set(renderers) == routable, (
        "os.js can draw extras oswin.js will not route, or the other way round — one is an empty "
        "window and the other is the timeline under somebody else's title")


def test_the_launcher_does_not_build_an_in_page_frame_for_an_extra_that_can_be_a_window():
    """`extra.act()` is the opener, and it makes an IN-PAGE frame. Showing one means raising the
    desktop's own full-output surface over every native window — the reported bug. So where a real
    toplevel is possible the opener must not run at all."""
    assert "if(extra && !_extraOpensAsWindow(view)){" in OS, (
        "openApp calls the in-page opener for every extra again, so opening one hides the windows "
        "behind it")
    guard = OS[OS.index("function _extraOpensAsWindow(view){"):][:600]
    assert "EXTRA_RENDER[extra.view]" in guard, (
        "a screen can be handed a window before anything can draw it")
    assert "PCOSWin.enabled()" in guard, (
        "web, Android and a desktop with no compositor would lose the in-page frame and get nothing")


@pytest.mark.parametrize("view,painter", sorted(PAINTERS.items()))
def test_the_in_page_frame_still_runs_the_painters_teardown(view, painter):
    """The painters own polling timers. In a frame, closing the window is what stops them — so the
    opener has to wire the teardown it is now handed, or Task Manager keeps calling the system
    bridge twice a second for the life of the desktop with nothing on screen."""
    assert f"w.onClose={painter}(w.slot" in OS, (
        f"{painter}'s teardown is not wired to its in-page window: its timer outlives the window")


def test_a_second_paint_stops_the_first():
    """A window CAN be re-routed while it lives — a monitor handoff re-enters the route, and so does
    launching the same app again. Without this the old timer keeps running against a host it no
    longer owns: two Task Managers' worth of snapshots a second, one of them invisible."""
    block = OS[OS.index("function _paintExtraInFeed(cls, paint){"):][:1400]
    assert "host._pcExtraStop()" in block, "re-painting an extra leaks the previous painter's timer"
    assert block.index("host._pcExtraStop()") < block.index("const stop = paint(host)"), (
        "the previous painter is stopped after the new one starts, so both run at once")


@pytest.mark.parametrize("cls", ["feed-taskmgr", "feed-vms", "feed-remote"])
def test_the_window_host_is_styled_for_a_whole_app(cls):
    """In a frame these apps got the whole body from an `.osw-*` rule. A window document has no
    `.osw-body`, so without a host rule the app paints into `#feed` — a padded, scrolling column —
    and scrolls twice."""
    css = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
    assert f".{cls}{{" in css, f"{cls} is set on the window host and the stylesheet defines nothing"
    rule = css[css.index(f".{cls}{{"):]
    rule = rule[:rule.index("}") + 1]
    assert "height:100%" in rule and "padding:0" in rule


@pytest.mark.skipif(NODE is None, reason="needs node")
@pytest.mark.parametrize("painter", sorted(PAINTERS.values()))
def test_a_painter_runs_with_no_window_around_it_and_its_teardown_stops_it(painter):
    """THE CLAIM, RUN. Everything above reads source; this calls the shipped painter the way a
    window document calls it — no `w`, no frame, no desktop — and then checks that the teardown it
    answered with actually stops the polling.

    A painter that still reached for the window would throw here, and a teardown that forgot its
    timer would leave one behind — neither of which a text assertion can see."""
    script = f"""
      import {{ runPainter }} from './tests/client/extra_painter_runtime.mjs';
      const r = runPainter({json.dumps(painter)});
      const before = r.timers();
      r.stop();
      console.log(JSON.stringify({{ painted: r.painted.length, before, after: r.timers(),
                                    stop: typeof r.stop, calls: r.calls }}));
    """
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert got["painted"] > 400, "the painter drew nothing into a host with no window around it"
    assert got["stop"] == "function", "the painter did not answer with a teardown"
    assert got["after"] == 0, "the teardown left a polling timer running"


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_remote_desktop_arms_with_the_paint_and_disarms_with_the_teardown():
    """Arming used to be done by the OPENER and cleared by the frame's onClose. A real toplevel has
    neither, so screen sharing would have been armed with nothing left to disarm it — the one bug in
    this family that is a security question rather than a cosmetic one."""
    script = """
      import { runPainter } from './tests/client/extra_painter_runtime.mjs';
      const r = runPainter('paintRemoteDesktop');
      const armed = r.calls.slice();
      r.stop();
      console.log(JSON.stringify({ armed, after: r.calls }));
    """
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert "armed:true" in got["armed"], "painting Remote Desktop no longer arms screen sharing"
    assert "armed:false" not in got["armed"], "it disarmed before anyone could share"
    assert "armed:false" in got["after"], (
        "the teardown does not disarm screen sharing — a closed window would leave this machine "
        "answering share invitations")
    assert "host:null" in got["after"], "the session host outlives the window that held it"


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_renderExtra_paints_routes_and_never_leaves_two_timers_running():
    """THE WHOLE PATH, RUN — `renderExtra` is what a window document calls, and this drives it
    against a stub `#feed` the way one would.

    Three rules at once, none of which source-reading can settle: the extra is drawn (a host class
    and real markup, not an empty window), a name it cannot draw answers FALSE so the ordinary
    landing still happens, and re-routing an already-painted window ends with ONE polling timer and
    not two. That last one is the whole reason the teardown exists; a leaked `setInterval` keeps
    calling the system bridge against an invisible host for the life of the window."""
    script = """
      import { runRenderExtra } from './tests/client/extra_painter_runtime.mjs';
      console.log(JSON.stringify(await runRenderExtra(['__tasks', '__nope', '__vms', '__remote'])));
    """
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    steps = {s["view"]: s for s in json.loads(done.stdout.strip().splitlines()[-1])["steps"]}

    assert steps["__tasks"]["answered"] is True
    assert steps["__tasks"]["cls"] == "feed feed-taskmgr", (
        "the window host is not styled for a whole app — the Task Manager paints into a padded, "
        "scrolling column")
    assert steps["__tasks"]["html"] > 400, "an empty window under the right title"

    assert steps["__nope"]["answered"] is False, (
        "renderExtra claimed a name it cannot draw; the caller then skips its own landing and the "
        "window shows whatever was already there")

    assert steps["__vms"]["cls"] == "feed feed-vms"
    for view in ("__tasks", "__vms", "__remote"):
        assert steps[view]["timers"] <= 1, (
            f"after routing to {view} there are {steps[view]['timers']} polling timers running — "
            "re-routing a window leaks the previous painter")


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_the_shipped_FS_rule_answers_correctly_for_every_surface():
    """RUN the rule rather than read it. The four shapes it has to tell apart look alike in source
    and behave completely differently on the machine, and the one that shipped broken — a window
    document with a perfectly good opener — is the one every app on PosterChanOS is in.

    ONE WRITER PER DEVICE still holds: what a child gets is the PRIMARY's bridge, so the pick, the
    grant and the sweep all belong to the primary surface. The opener CHAIN is followed to find it —
    on a second monitor the chain is window → secondary → primary — and a chain containing no
    primary hands back nothing rather than electing a secondary."""
    body = fs_body()
    script = f"""
      const mk = (over) => Object.assign({{ pcFs: null, pcShell: null, opener: null }}, over);
      {fs_deps()}
      const run = (w) => {{
        const window = w;
        const FS = {body[body.index("() => {"):].rstrip().rstrip(";")};
        return FS();
      }};
      const primary = mk({{ pcFs: 'PRIMARY' }});
      const secondary = (opener) => mk({{ pcFs: 'OWN', pcShell: {{ backgroundOwner: false }}, opener }});
      const thrower = mk({{ pcFs: 'OWN', pcShell: {{ backgroundOwner: false }} }});
      Object.defineProperty(thrower, 'opener', {{ get(){{ throw new Error('cross-origin'); }} }});
      console.log(JSON.stringify({{
        primary: run(primary),
        shellPrimary: run(mk({{ pcFs: 'PRIMARY', pcShell: {{ backgroundOwner: true }} }})),
        borrows: run(secondary(primary)),
        noOpener: run(secondary(null)),
        closedOpener: run(secondary(Object.assign(mk({{ pcFs: 'PRIMARY' }}), {{ closed: true }}))),
        openerIsAWindow: run(secondary(secondary(primary))),
        openerThrows: run(thrower),
        chainWithNoPrimary: run(secondary(secondary(secondary(null)))),
      }}));
    """
    done = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])

    assert got["primary"] == "PRIMARY", "the desktop itself lost its own bridge"
    assert got["shellPrimary"] == "PRIMARY", "the shell surface lost its own bridge"
    assert got["borrows"] == "PRIMARY", (
        "a popped-out window still refuses the opener's bridge — this is 'Set up on this device' "
        "rendering disabled and clicking silently, in the only place anybody ever sees it")
    assert got["noOpener"] is None
    assert got["closedOpener"] is None, "a dead desktop is not a bridge"
    # A CHAIN IS FOLLOWED TO THE PRIMARY, AND THE RULE IS UNCHANGED BY THAT.
    #
    # This used to expect None: an opener that was itself a secondary surface was refused outright.
    # On a multi-monitor PosterChanOS the windows on the second screen are opened from a window that
    # IS a secondary, so the chain is window → secondary → primary — and refusing at the first hop
    # disabled "Set up on this device" on every screen but one, silently, which is the same report
    # this whole file exists for.
    #
    # One writer per device still holds, because what comes back is still the PRIMARY's bridge and
    # never a secondary's: `chainWithNoPrimary` below is the assertion that says so.
    assert got["openerIsAWindow"] == "PRIMARY", (
        "a window two hops from the primary could not reach it, so Folder Sync is disabled on every "
        "screen but one")
    assert got["chainWithNoPrimary"] is None, (
        "a chain containing no primary elected a writer anyway — that is two writers on one device, "
        "which is the rule this gate exists for")
    assert got["openerThrows"] is None, "a cross-origin opener must refuse, not throw"
