from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_native_launcher_apps_create_a_window_instead_of_routing_back_to_themselves():
    """Task Manager is an EXTRA, so an ordinary openApp call recursively invokes act()."""
    src = (ROOT / "static/js/client/os.js").read_text()
    assert "const extra = !direct && EXTRAS.find" in src
    assert "openApp('__tasks','Task Manager','#i-chart',null,true,true)" in src


def test_the_old_local_vms_shortcut_opens_virtual_machines():
    """The desktop-only "Local VMs" painter is gone: this machine's VMs are "This computer" on the
    Virtual Machines screen (`vms`). Anything that saved the old name — a pin, a handoff — lands there."""
    src = (ROOT / "static/js/client/os.js").read_text()
    assert "openApp('__vms'" not in src and "function paintVmManager" not in src
    assert "view: '__vms'" not in src, "a second start-menu entry for the same screen"
    assert "const LEGACY_VIEWS = { __vms: 'vms' };" in src
    body = src[src.index("function openApp(view, label, icon, render, noFeed, direct){"):]
    assert body.split("\n", 2)[1].strip() == "view = legacyView(view);", "openApp resolves the old name first"
    app = (ROOT / "static/js/client/app.js").read_text()
    sv = app[app.index("function switchView(v, quiet){"):]
    assert "if(v === '__vms') v = 'vms';" in sv[:sv.index("_viewNeedsInstance(v)")]
