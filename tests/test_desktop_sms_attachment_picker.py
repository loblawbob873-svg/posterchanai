from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()
SMS = (ROOT / "static/js/client/sms.js").read_text()


def test_desktop_picker_is_user_confirmed_bounded_and_image_filtered():
    block = MAIN.split("ipcMain.handle('pc:host:pickFile'", 1)[1].split(
        "ipcMain.handle(", 1)[0]
    assert "dialog.showOpenDialog" in block
    assert "properties:['openFile']" in block
    assert "o.images" in block
    assert "64*1024*1024" in block
    assert "fs.readFileSync(file)" in block


def test_preload_returns_browser_safe_bytes_not_a_node_buffer():
    assert "pickFile: (opts) => ipcRenderer.invoke('pc:host:pickFile'" in PRELOAD
    assert "data:new Uint8Array(r.data)" in PRELOAD


def test_texts_device_source_uses_native_picker_on_desktop_and_keeps_web_fallback():
    block = SMS.split("const fromDevice = async () =>", 1)[1].split(
        "attachBtn.onclick = async () =>", 1
    )[0]
    assert "pcHost.pickFile" in block
    assert "new File([chosen.data],chosen.name" in block
    assert "pick.click()" in block


def test_desktop_does_not_bypass_account_files_attachment_source():
    block = SMS.split("attachBtn.onclick = async () =>", 1)[1].split("pick.onchange", 1)[0]
    assert "PC.blossomPicker && PC.modal" in block
    assert 'id="sms-src-device"' in block
    assert 'id="sms-src-blossom"' in block
    assert "#sms-src-device').onclick=()=>{PC.closeModal();fromDevice();}" in block
    assert "#sms-src-blossom').onclick=()=>{PC.closeModal();fromBlossom();}" in block
    assert "if(window.pcHost && pcHost.pickFile)" not in block
