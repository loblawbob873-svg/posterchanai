from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


def _compose():
    start = APP.index("function compose({reply=")
    end = APP.index("// ---------- Blossom uploads", start)
    return APP[start:end]


def test_browser_paperclip_opens_native_file_input_without_intermediate_popover():
    """Firefox must retain the trusted click needed to open its native file chooser."""
    body = _compose()
    handler = body[body.index("$('#cmp-attach',root).onclick"):]
    direct = "if(!window.Capacitor){ $('#cmp-file',root).click(); return; }"
    assert direct in handler
    assert handler.index(direct) < handler.index("openMenuPopover($('#cmp-attach',root)")


def test_existing_files_remain_available_from_composer_more_menu():
    body = _compose()
    assert "items.push(['files','📁 Attach from Files'])" in body
    assert "if(a==='files'){ blossomPicker(ta); return; }" in body
