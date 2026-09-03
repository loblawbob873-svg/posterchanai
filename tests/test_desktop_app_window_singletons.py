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
        "created.webContents.setWindowOpenHandler", 1
    )[0]
    assert "pcAppWindows.set(view, child)" in created
    assert "child.once('closed'" in created
    assert "pcAppWindows.delete(view)" in created


def test_every_open_routes_an_existing_app_before_requesting_a_child():
    body = OSWIN.split("function open(view, label, opts){", 1)[1].split(
        "function routeExisting", 1
    )[0]
    assert body.index("routeExisting(view)") < body.index("root.open(")


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
