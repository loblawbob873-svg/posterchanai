from pathlib import Path


def test_ephemeral_popup_close_cannot_be_vetoed_by_page_unload():
    main = (Path(__file__).resolve().parents[1] / "desktop/main.js").read_text()
    block = main[main.index("function closePopupWindow()") : main.index("ipcMain.handle('pc:popup:toggle'")]
    assert "if(STICKY_POPUPS.has(kind)) p.close(); else p.destroy();" in block
    assert "_popupWin = null; _popupKind = '';" in block
