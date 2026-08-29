from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def vm_delete_handler():
    start = OS.index("list.querySelectorAll('[data-vm-delete]')")
    return OS[start:OS.index("}catch(e){", start)]


def test_vm_delete_uses_an_in_app_confirm_owned_by_its_managed_window():
    handler = vm_delete_handler()
    assert "confirm(" not in handler
    assert "await PC().uiConfirm" in handler
    assert "owner:w.body" in handler
    assert "ok:'Delete'" in handler and "danger:true" in handler


def test_cancel_returns_before_any_vm_or_geometry_mutation_and_confirm_targets_only_selected_vm():
    handler = vm_delete_handler()
    assert handler.index("if(!ok)return") < handler.index("pcVM.remove(n,true)")
    assert "pcVM.remove(n,true)" in handler
    for broad in ("removeAll", "querySelector('.osw')", "releaseFeed", "exit()"):
        assert broad not in handler


def test_confirm_supports_a_connected_owner_without_changing_browser_default_scope():
    block = APP[APP.index("function uiConfirm("):APP.index("function _copyFallback", APP.index("function uiConfirm("))]
    assert "opts.owner&&opts.owner.isConnected" in block
    assert "owner!==document.body" in block
    assert "owner.appendChild(ov)" in block
    assert ".uiconfirm-bg.uiconfirm-owned{position:absolute}" in CSS
    assert ".osw-body:has(>.uiconfirm-owned){position:relative}" in CSS


def test_owned_delete_dialog_is_sized_by_the_vm_window_not_the_desktop_viewport():
    owned = CSS[CSS.index(".uiconfirm-owned .uiconfirm{"):
                CSS.index("/* ===== Node Control", CSS.index(".uiconfirm-owned .uiconfirm{"))]
    assert "width:min(400px,100%)" in owned
    assert "max-width:100%" in owned
    assert ".uiconfirm-owned .uiconfirm-btns{flex-wrap:wrap}" in owned
    assert "flex:1 1 110px" in owned
    assert ".uiconfirm{width:min(400px,94vw)" in CSS[:CSS.index(".uiconfirm-owned .uiconfirm{")]
