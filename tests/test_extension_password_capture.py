"""Regression checks for submitted-password capture policy."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")

def _read(name):
    with open(os.path.join(EXT, name), encoding="utf-8") as fh:
        return fh.read()

def test_save_decision_waits_for_an_explicit_answer():
    offer = _read("content.js").split("async function offerSave", 1)[1]
    assert "20000" not in offer
    assert "setTimeout(close" not in offer
    assert "bar.querySelector('.pcpw-no').onclick = close" in offer
    assert "bar.querySelector('.pcpw-yes').onclick = async" in offer

def test_auto_save_is_opt_in_durable_and_exposed():
    bg, content = _read("background.js"), _read("content.js")
    popup, html = _read("popup.js"), _read("popup.html")
    assert "let autoSavePasswords = false" in bg
    assert "got.autoSavePasswords === true" in bg
    assert "B.storage.local.set({ autoSavePasswords })" in bg
    setting = bg.split("case 'auto-save-set'", 1)[1].split("case ", 1)[0]
    assert "if(!_fromPopup(sender))" in setting
    assert "autoSave: autoSavePasswords" in bg
    assert "if(res.autoSave){ await save(); return; }" in content
    assert 'id="auto-save-passwords"' in html
    assert "type:'auto-save-set', on:wanted" in popup
