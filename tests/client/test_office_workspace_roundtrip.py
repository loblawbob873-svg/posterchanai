from pathlib import Path
from tests.client_source import client_source


ROOT = Path(__file__).resolve().parents[2]
APP = client_source()
SHELL = (ROOT / "templates/client.html").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
HOSTFILES = (ROOT / "static/js/client/hostfiles.js").read_text(encoding="utf-8")


def test_office_is_a_real_launcher_not_only_a_files_toolbar_action():
    assert 'data-view="office"' in SHELL
    assert "if (VIEW==='office') return renderOfficeHome()" in APP
    assert "function renderOfficeHome()" in APP
    assert 'data-office-new=' in APP
    assert 'id="office-open-files"' in APP
    assert 'id="office-open-local"' in APP
    deep_links = APP[APP.index("const VALID = new Set"):APP.index("if(view && VALID.has(view)")]
    assert "'office'" in deep_links


def test_editor_save_as_and_pdf_share_the_destination_picker():
    session = APP[APP.index("async function _officeSession"):APP.index("function renderOfficeHome")]
    assert 'id="office-saveas"' in session
    assert session.count("_officeSaveCopy(") >= 2
    picker = APP[APP.index("function _chooseSaveTarget"):APP.index("function renderOfficeHome")]
    # The sheet is a name plus a PLACE you browse to (2026-10-21: "Save as does not let me choose a folder"):
    # a drive folder, a folder inside any synced folder, or this computer.
    assert "data-sv-go=" in picker
    for destination in ("kind:'drive'", "kind:'sync'", "kind:'local'"):
        assert destination in picker, f"the {destination} destination is gone from the picker"
    assert "PCSync.edit.uploadMany" in picker
    assert "_officeStoreDrive" in picker
    assert "pcHost.saveFile" in picker
    assert "else await saveBlobAs(blob,t.name)" in picker, (
        "Android has no desktop pcHost bridge; its local destination must reach saveBlobAs, "
        "which hands the PDF/document to the native Files/share sheet"
    )


def test_desktop_picker_keeps_the_path_and_offers_a_native_save_dialog():
    assert "path:String(r.path||'')" in PRELOAD
    assert "mtime:Number(r.mtime)||0" in PRELOAD
    assert "pc:host:saveFile" in PRELOAD
    assert "path:file" in MAIN
    assert "ipcMain.handle('pc:host:saveFile'" in MAIN
    assert "dialog.showSaveDialog" in MAIN
    assert "pcHost.writeBytes(p.path" in APP
    assert "openedMtime=p.mtime||0" in APP
    assert "openHere.mtime = Number(meta.mtime) || 0" in HOSTFILES
    assert "fs.renameSync(tmp,r.filePath)" in MAIN
