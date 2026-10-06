"""xterm loads when a terminal opens, not with every page.

The client shell carried three eager tags for xterm (289 kB of emulator + addon + CSS) that every visit
parsed for one screen most people never open. term.js now fetches them on first use. What keeps that
safe is pinned here: nothing else in the client uses xterm, the service worker still precaches the
files (offline terminals), and both native bundles ship the whole vendor tree. That the terminal still
OPENS is proven by the full-app terminal tests (test_desktop_offline_full_app,
test_terminal_clears_the_bottom_nav_full_app), which drive the real screen.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_shell_no_longer_loads_xterm_up_front():
    html = (ROOT / "templates/client.html").read_text()
    assert not re.search(r'<(script|link)[^>]+vendor/xterm/', html), "xterm is back in the client shell"


def test_term_js_loads_it_from_the_same_place_and_version():
    js = (ROOT / "static/js/client/term.js").read_text()
    assert "function _loadXterm()" in js
    for f in ("xterm.css", "xterm.js", "fit.js"):
        assert f"'{f}'" in js or f"+ '{f}' +" in js or f"'{f}' + q" in js, f
    # every mount waits for it
    assert js.count("await _loadXterm()") >= 3


def test_nothing_else_touches_xterm():
    for f in (ROOT / "static/js/client").glob("*.js"):
        if f.name == "term.js":
            continue
        assert "window.Terminal" not in f.read_text() and "FitAddon" not in f.read_text(), f.name


def test_offline_and_the_native_bundles_still_have_it():
    sw = (ROOT / "static/js/client/sw.js").read_text()
    for f in ("xterm.css", "xterm.js", "fit.js"):
        assert f"/static/vendor/xterm/{f}" in sw, f"{f} is not precached: an offline terminal breaks"
    for build in ("desktop/build-www.sh", "mobile/build-www.sh"):
        assert re.search(r'cp -r "\$SRC"/static/vendor\s', (ROOT / build).read_text()), build
