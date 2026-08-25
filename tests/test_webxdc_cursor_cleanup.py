"""A Webxdc game's pointer lock must not leak its cursor state into other apps/windows."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/client/webxdc.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


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
