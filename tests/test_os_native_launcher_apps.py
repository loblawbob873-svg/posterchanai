from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_native_launcher_apps_create_a_window_instead_of_routing_back_to_themselves():
    """Task Manager and VMs are EXTRAS, so an ordinary openApp call recursively invokes act()."""
    src = (ROOT / "static/js/client/os.js").read_text()
    assert "const extra = !direct && EXTRAS.find" in src
    assert "openApp('__tasks','Task Manager','#i-chart',null,true,true)" in src
    assert "openApp('__vms','Virtual Machines','#i-monitor',null,true,true)" in src
