"""keys.js ships with the page, BEFORE app.js, and is built synchronously while app.js boots.

The keyboard code — the Alt+<letter> shortcuts and their help sheet, vim movement, the card-cursor
keys and keyboard scrolling — moved out of app.js into keys.js. Its top-level statements ARE the
global keydown/focus listeners, so a module built on first use would leave the keyboard dead until
something happened to call into it (and nothing would, because the listeners are how anything calls
into it). So:

  * templates/client.html loads keys.js with its own <script> tag, BEFORE app.js (the bundle builds,
    desktop/build-www.sh and mobile/build-www.sh, copy every client .js and render this template, so
    they inherit the order);
  * app.js builds the module (`_keysMod()`) at the point where the code used to sit, so its listeners
    are registered in the same order relative to app.js's other capture-phase listeners as before;
  * the service worker precaches it, or an offline PWA boots with no keyboard at all;
  * `_vimPane` stays declared in app.js — switchView and settings.js reset it too — and the module
    reaches it through a getter AND a setter (a getter alone would silently drop every h/l move).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
TEMPLATE = (ROOT / "templates" / "client.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
KEYS = (CLIENT / "keys.js").read_text(encoding="utf-8")


def _script_order(html):
    return [m.group(1) for m in re.finditer(r'<script[^>]*\bsrc="/static/js/client/([\w.-]+\.js)', html)]


def test_keys_js_is_loaded_before_app_js():
    order = _script_order(TEMPLATE)
    assert "keys.js" in order, "templates/client.html does not load keys.js"
    assert order.index("keys.js") < order.index("app.js"), (
        "keys.js must be loaded BEFORE app.js — app.js builds it synchronously while it boots")
    assert order.count("keys.js") == 1


def test_keys_js_is_precached_by_the_service_worker():
    sw = (CLIENT / "sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL"):sw.index("];", sw.index("const SHELL"))]
    assert "'/static/js/client/keys.js'" in shell


def test_app_js_builds_keys_eagerly_not_on_first_use():
    assert "_lzGet('keys.js', 'PCKeysFactory', _keysDeps)" in APP
    # A bare `_keysMod();` statement at the IIFE's top level — not inside an entry point.
    assert re.search(r"^  _keysMod\(\);", APP, re.M), "app.js no longer builds keys.js while it boots"
    # ...after its deps function exists and BEFORE window.__PC is published.
    assert APP.index("function _keysDeps(") < APP.index("\n  _keysMod();") < APP.index("window.__PC = {")
    assert KEYS.lstrip().startswith("/*") and "window.PCKeysFactory = function(dep){" in KEYS
    # The listeners really are top-level statements of the factory (i.e. they run when it is BUILT).
    assert re.search(r"^  document\.addEventListener\('keydown'", KEYS, re.M)


def test_every_keyboard_entry_point_is_a_forwarder_the_module_returns():
    """Every function app.js (and, through app.js's deps objects, cards.js / files.js) calls by name
    must be forwarded to keys.js, and keys.js must return it."""
    forwarded = set(re.findall(r"function (\w+)\(\)\{ return _lzRun\(_keysMod, _keysLoad, '(\w+)'", APP))
    names = {a for a, b in forwarded if a == b}
    assert names == {a for a, _ in forwarded}
    for must in ("_selectNote", "_selEl", "_lbGroup", "_vimOn"):
        assert must in names, f"{must} is no longer forwarded to keys.js"
    ret = KEYS[KEYS.rindex("  return {"):]
    for n in names:
        assert re.search(r"\b" + re.escape(n) + r"\b", ret), f"keys.js does not return {n}"
    # The real definitions live ONLY in keys.js — app.js keeps nothing but the forwarder.
    assert "function _selectNote(el" in KEYS and "function _selectNote(el" not in APP
    assert "function _vimOn(){ return !!ClientSettings.get('vimKeys', false); }" in KEYS


def test_vim_pane_stays_in_app_js_and_the_module_can_write_it():
    assert re.search(r"^  let _vimPane='feed';", APP, re.M), "_vimPane must stay declared in app.js"
    assert not re.search(r"\blet _vimPane\b", KEYS)
    deps = APP[APP.index("function _keysDeps(){"):APP.index("function _keysMod(")]
    assert "get _vimPane(){ return _vimPane; }, set _vimPane(v){ _vimPane = v; }" in deps
    # The module never reads or writes a bare `_vimPane` (a captured copy would freeze it).
    assert not re.search(r"(?<![\w$.])_vimPane(?![\w$])", KEYS.replace("S._vimPane", ""))
    # switchView still resets it in app.js's own scope.
    assert "_selectNote(null); _vimPane='feed';" in APP
