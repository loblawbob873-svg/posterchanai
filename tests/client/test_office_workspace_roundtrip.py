from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
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
    picker = APP[APP.index("function _officeSaveCopy"):APP.index("function renderOfficeHome")]
    # The three destinations are generated from a list now (so the sheet can use the shared
    # `.ow-opt` chooser markup — see tests/test_office_reaches_the_editor.py), so assert the
    # destinations themselves rather than the attribute text a template no longer contains.
    assert "data-office-dest=" in picker
    for destination in ("'drive'", "'sync'", "'local'"):
        assert destination in picker, f"the {destination} destination is gone from the picker"
    assert "PCSync.edit.uploadMany" in picker
    assert "_officeStoreDrive" in picker
    assert "pcHost.saveFile" in picker
    assert "else await saveBlobAs(blob,name)" in picker, (
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
