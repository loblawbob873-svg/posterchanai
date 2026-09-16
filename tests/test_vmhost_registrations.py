"""Every door into Virtual Machines exists — the view is useless on any platform where it has no way in.

The client has several independent registries for a view and each has been the one somebody forgot:
the sidebar row (which is also how the DESKTOP builds its icons and start menu), the stale-shell heal
(`_NAV_REQUIRED`), the phone's More sheet (the sidebar is hidden on a phone), the deep-link allowlist,
the module router, the Android launcher catalogue, and the service worker's precache. And two it must
stay OUT of: `INSTANCE_VIEWS` (a key-and-relays-only bundle must still reach VM hosts) and the
`nostr_only` template guard. The launcher tile's LANDING is covered generically by
tests/client/test_every_launcher_tile_lands_on_its_screen.py, which reads the catalogue.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates/client.html").read_text(encoding="utf-8")
SW = (ROOT / "static/js/client/sw.js").read_text(encoding="utf-8")
TILES = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java").read_text()
MAIN = (ROOT / "app/main.py").read_text(encoding="utf-8")


def _block(src, start, end):
    i = src.index(start)
    return src[i: src.index(end, i)]


def test_the_sidebar_row_exists_and_is_not_nostr_only_guarded():
    line = next(l for l in HTML.splitlines() if 'data-view="vms"' in l)
    assert "nostr_only" not in line, "VM hosts are reached over Nostr — a nostr-only node must show it"
    assert "#i-monitor" in line


def test_the_client_registries():
    assert "'vms'" in _block(APP, "const VALID = new Set([", "]);")
    assert "view:'vms'" in _block(APP, "const _NAV_REQUIRED = [", "];")
    assert "['vms','monitor','Virtual Machines']" in APP, "the phone's More sheet"
    assert "renderModuleView('vms','vms.js','PCVms','render')" in APP
    assert "'vms'" not in _block(APP, "const INSTANCE_VIEWS = new Set([", "]);"), \
        "a server-less bundle must keep Virtual Machines"
    back = _block(APP, "_App.addListener('backButton'", "if(typeof VIEW!=='undefined' && VIEW){")
    assert "PCVms.consoleOpen()" in back and "PCVms.closeConsole()" in back


def test_the_launcher_tile():
    assert re.search(r'new Tile\("vms",\s*"Virtual Machines",\s*"monitor",\s*false\)', TILES)
    assert (ROOT / "mobile/android/app/src/main/res/drawable/ic_pc_monitor.xml").exists()


def test_the_modules_are_precached_but_not_boot_payload():
    for f in ("vms.js", "vmrpc.js", "vmconsole.js"):
        assert f"'/static/js/client/{f}'" in SW, f"{f} missing from the SW precache"
        assert f"/static/js/client/{f}" not in HTML, \
            f"{f} is loaded on demand; a page tag would count against the boot payload budget"


def test_novnc_is_vendored_with_its_licence_and_version():
    base = ROOT / "static/vendor/novnc"
    for rel in ("core/rfb.js", "core/websock.js", "core/inflator.js", "vendor/pako/lib/zlib/inflate.js",
                "LICENSE.txt", "README.md"):
        assert (base / rel).is_file(), rel
    readme = (base / "README.md").read_text()
    assert "1.5.0" in readme and re.search(r"[0-9a-f]{64}", readme)
    assert not (base / "app").exists(), "only the core library is vendored"


def test_the_app_wires_the_routers_and_the_service_lifecycle():
    assert "app.include_router(vmhost_router.ws_router)" in MAIN
    assert "app.include_router(vmhost_router.router)" in MAIN
    start = MAIN.index("vmhost_transport.start()")
    guard = MAIN.index("        if app_port == 3051:\n")
    other_ports = MAIN.index("Schedulers disabled on port")
    assert guard < start < other_ports, "the host runs only inside the port-3051 startup branch"
    assert "await vmhost_transport.stop()" in MAIN


def test_the_admin_tab_is_reachable():
    admin = (ROOT / "templates/admin.html").read_text()
    system = _block(admin, '<span class="tab-group-label">System</span>', "</div>")
    assert 'data-tab="vms"' in system
