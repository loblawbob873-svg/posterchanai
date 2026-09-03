from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text()
WIN = (ROOT / "static/js/client/oswin.js").read_text()


def test_existing_native_app_is_routed_before_it_is_focused():
    branch = OS.split("const mine = nativeTasks.find", 1)[1].split("const existing = wins.find", 1)[0]
    assert "PCOSWin.routeExisting(view)" in branch
    assert branch.index("PCOSWin.routeExisting(view)") < branch.index("pcWM.show")


def test_window_route_crosses_renderer_and_monitor_boundaries():
    assert "new root.BroadcastChannel(ROUTE_CHANNEL)" in WIN
    assert "ch.postMessage({view:v})" in WIN
    assert "String(state.view||'')!==v" in WIN
    assert "root.__PC.switchView(v)" in WIN
    assert "routeExisting" in WIN.split("const API =", 1)[1]
