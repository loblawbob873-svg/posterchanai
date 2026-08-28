"""A Webxdc game's pointer lock must not leak its cursor state into other apps/windows."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/client/webxdc.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
LOADER = (ROOT / "static/webxdc-sandbox/index.html").read_text()


def test_destroy_releases_only_the_session_frame_pointer_lock_before_removal():
    body = JS[JS.index("Session.prototype.destroy = function()"):
              JS.index("Session.prototype.post = function")]
    assert "document.pointerLockElement === this.frame" in body
    assert "document.exitPointerLock()" in body
    assert body.index("document.exitPointerLock()") < body.index("this.frame.remove()")


def test_pointer_lock_listener_is_removed_with_the_session():
    destroy = JS[JS.index("Session.prototype.destroy = function()"):
                 JS.index("Session.prototype.post = function")]
    mount = JS[JS.index("Session.prototype.mount = async function"):
               JS.index("Session.prototype", JS.index("Session.prototype.mount = async function") + 20)]
    assert "addEventListener('pointerlockchange', this._onPointerLock)" in mount
    assert "removeEventListener('pointerlockchange', this._onPointerLock)" in destroy


def test_unlocked_game_frame_uses_the_normal_cursor():
    assert ".xdc-frame{" in CSS
    rule = CSS.split(".xdc-frame{", 1)[1].split("}", 1)[0]
    assert "cursor:auto" in rule


def test_pointer_lock_is_delegated_through_both_cross_origin_frame_levels():
    mount = JS[JS.index("Session.prototype.mount = async function"):
               JS.index("Session.prototype.reply = function")]
    assert "f.setAttribute('allow', 'autoplay; fullscreen; gamepad; pointer-lock')" in mount
    # Worker and blob startup paths both create the inner application frame.
    assert LOADER.count("setAttribute('allow', 'autoplay; fullscreen; gamepad; pointer-lock')") == 2


def test_game_document_is_focused_inside_the_same_pointer_gesture():
    """An event in an iframe does not bubble to its iframe element.  The capture listener must be
    installed in the same-origin app document itself, before the engine's click handler requests
    pointer lock, and the no-service-worker fallback needs the identical path."""
    assert "f.contentDocument.addEventListener('pointerdown', _focusInner, true)" in LOADER
    assert "f2.contentDocument.addEventListener('pointerdown', _focusF2, true)" in LOADER
    assert LOADER.index("f.contentDocument.addEventListener('pointerdown'") < LOADER.index("post({ jsonrpc:'2.0', method:'sandbox.running' })")
