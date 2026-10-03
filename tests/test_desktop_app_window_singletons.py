from pathlib import Path
import json
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
OSWIN = (ROOT / "static/js/client/oswin.js").read_text(encoding="utf-8")


def test_native_app_singletons_are_enforced_process_wide():
    """The main process, shared by every monitor renderer, owns deduplication."""
    assert "const pcAppWindows = new Map()" in MAIN
    claim = MAIN.split("function claimPcAppWindow(raw) {", 1)[1].split(
        "/* Tray / background state.", 1
    )[0]
    assert "const prior = pcAppWindows.get(view)" in claim
    assert "if (prior.pending) return true" in claim
    assert "pcAppWindows.set(view, reservation)" in claim
    handler = MAIN.split("created.webContents.setWindowOpenHandler", 1)[1]
    assert "claimPcAppWindow(url)) return DENY_WINDOW_OPEN" in handler


def test_pending_creation_is_replaced_and_closed_windows_are_released():
    created = MAIN.split("created.webContents.on('did-create-window'", 1)[1].split(
        "child.once('closed'", 1
    )[0]
    assert "pcAppWindows.set(view, child)" in created
    closed = MAIN.split("child.once('closed'", 1)[1].split(
        "created.webContents.setWindowOpenHandler", 1
    )[0]
    assert "pcAppWindows.delete(view)" in closed


def test_every_open_routes_an_existing_app_before_requesting_a_child():
    body = OSWIN.split("function open(view, label, opts){", 1)[1].split(
        "function routeExisting", 1
    )[0]
    # The call carries more arguments since the handoff learned `arg`; the rule is the ORDER.
    routed = re.search(r"routeExisting\(view\b", body)
    assert routed, body[:400]
    assert routed.start() < body.index("root.open(")


def test_every_registered_app_is_deduplicated_by_the_shipped_claim_logic():
    views = sorted(set(re.findall(
        r'data-view=["\']([^"\']+)',
        (ROOT / "templates/client.html").read_text(encoding="utf-8"),
    )))
    policy = MAIN.split("const pcAppWindows = new Map();", 1)[1].split(
        "/* Tray / background state.", 1
    )[0]
    script = f"""
      const pcAppWindows = new Map();
      const setTimeout = () => 0;
      {policy}
      const views = {json.dumps(views)};
      const results = views.map(view => {{
        const url = 'app://posterchan/index.html?pcwin=' + encodeURIComponent(view);
        return [view, claimPcAppWindow(url), claimPcAppWindow(url)];
      }});
      console.log(JSON.stringify(results));
    """
    run = subprocess.run(
        ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=20
    )
    assert run.returncode == 0, run.stderr
    results = json.loads(run.stdout)
    assert len(results) > 20
    assert results == [[view, False, True] for view in views]


def test_a_renderer_can_ask_whether_an_app_already_has_its_window():
    """A shell surface sees only its own monitor's windows. When its request for an app window is
    refused because that window exists on the OTHER monitor, it must be able to learn that -- or it
    draws an in-page copy and raises the desktop over every window on its monitor ("If I click on
    Messages, Global disappears"). Runs the shipped policy: a claimed (pending) window counts, a live
    one counts, a destroyed one does not, and nothing else does."""
    policy = MAIN.split("const pcAppWindows = new Map();", 1)[1].split(
        "/* Tray / background state.", 1
    )[0]
    script = f"""
      const pcAppWindows = new Map();
      const setTimeout = () => 0;
      {policy}
      const url = v => 'app://posterchan/index.html?pcwin=' + v;
      const out = [hasPcAppWindow('messages')];
      claimPcAppWindow(url('messages'));
      out.push(hasPcAppWindow('messages'), hasPcAppWindow('global'));
      pcAppWindows.set('messages', {{isDestroyed: () => false}});
      out.push(hasPcAppWindow('messages'));
      pcAppWindows.set('messages', {{isDestroyed: () => true}});
      out.push(hasPcAppWindow('messages'), hasPcAppWindow(''), hasPcAppWindow(undefined));
      console.log(JSON.stringify(out));
    """
    run = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [False, True, False, True, False, False, False]
    assert "ipcMain.on('pc:win:has'" in MAIN
    assert "hasAppWindow: (view) => ipcRenderer.sendSync('pc:win:has'" in (ROOT / "desktop/preload.js").read_text()


def _raise_run(rows, title="PosterChan Window — terminal", shell=True):
    policy = MAIN.split("const pcAppWindows = new Map();", 1)[1].split("/* Tray / background state.", 1)[0]
    script = f"""
      const pcAppWindows = new Map();
      const setTimeout = () => 0;
      const SHELL_MODE = {json.dumps(shell)};
      const calls = [];
      const ROWS = {json.dumps(rows)};
      const wm = () => ({{ available: () => true, windows: async () => ROWS,
                          show: async id => calls.push(['show', id]), focus: async id => calls.push(['focus', id]) }});
      {policy}
      // On Wayland these Electron calls change nothing -- which is the bug; they are recorded only.
      const prior = {{ pending:false, isDestroyed:()=>false, isMinimized:()=>true, restore(){{}}, show(){{}}, focus(){{}},
                       getTitle:()=>{json.dumps(title)} }};
      pcAppWindows.set('terminal', prior);
      const claimed = claimPcAppWindow('app://posterchan/index.html?pcwin=terminal');
      setImmediate(() => setImmediate(() => setImmediate(() => console.log(JSON.stringify({{claimed, calls}})))));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_open_app_on_the_other_monitor_is_brought_back_by_the_compositor():
    """'if a posterchan app is open and you click on the app in the start menu or desktop, the user
    sees nothing' -- the launch reached main, whose Electron restore/focus are no-ops on Wayland."""
    rows = [{"id": 7, "title": "PosterChan Desktop", "stashed": False},
            {"id": 15, "title": "PosterChan Window — terminal", "stashed": True},
            {"id": 112, "title": "PosterChan Window — global", "stashed": False}]
    r = _raise_run(rows)
    assert r["claimed"] is True, r
    assert r["calls"] == [["show", 15], ["focus", 15]], r
    # Not minimised: focus only.
    rows[1]["stashed"] = False
    assert _raise_run(rows)["calls"] == [["focus", 15]]


def test_the_compositor_is_never_asked_about_an_ambiguous_or_missing_window():
    two = [{"id": 15, "title": "PosterChan Window — terminal"}, {"id": 16, "title": "PosterChan Window — terminal"}]
    assert _raise_run(two)["calls"] == []
    assert _raise_run([{"id": 3, "title": "Firefox"}])["calls"] == []
    # Off PosterChanOS (Windows/macOS app) there is no compositor to ask; Electron's own calls stand.
    assert _raise_run([{"id": 15, "title": "PosterChan Window — terminal"}], shell=False)["calls"] == []
