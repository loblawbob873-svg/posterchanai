from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
VMS = (ROOT / "static/js/client/vms.js").read_text(encoding="utf-8")


def vm_delete_handler():
    """The desktop's own "Local VMs" painter (os.js) is gone; deleting a VM on THIS computer is the same
    Virtual Machines screen as a server host — vms.js `del()`, over LocalHost's `vm.delete`."""
    start = VMS.index("async function del(pk, vm){")
    return VMS[start:VMS.index("\n  }", start)]


def test_vm_delete_uses_in_app_dialogs_never_native_ones():
    handler = vm_delete_handler()
    assert "confirm(" not in handler.replace("uiConfirm(", "") and "prompt(" not in handler.replace("uiPrompt(", "")
    assert "await PC.uiPrompt(" in handler and "await PC.uiConfirm(" in handler
    assert "danger: true" in handler
    assert "function paintVmManager" not in OS, "the retired Local VMs painter came back"


def test_cancel_returns_before_any_vm_mutation_and_the_delete_names_only_this_vm():
    handler = vm_delete_handler()
    call = handler.index("call(pk, 'vm.delete'")
    assert handler.index("if(typed == null) return;") < call
    assert handler.index("if(typed !== vm.name) return") < call, "a mistyped name must delete nothing"
    assert "{ vm: vm.uuid, confirm_name: typed, delete_disks: !!disks }" in handler
    local = VMS[VMS.index("case 'vm.delete': {"):]
    assert "vm.remove(a.vm, !!a.delete_disks)" in local[:local.index("}")], \
        "LocalHost deletes exactly the VM it was asked about, disks only when chosen"


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
