from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()


def _picker():
    start = APP.index("function blossomPicker(ta, onPick, opts={})")
    return APP[start:APP.index("\n  // ---------- Pics:", start)]


def test_all_files_builds_urls_for_standard_blossom_list_entries():
    body = _picker()
    assert "if(Array.isArray(rows))" in body
    assert "rows.filter(b=>b&&b.sha256).map" in body
    assert "url:b.url || (server.replace(/\\/$/,'')+'/'+b.sha256)" in body


def test_all_files_does_not_filter_to_only_root_folder():
    body = _picker()
    assert "cur==='' || (FilesIdx.folderOf(b.sha256)||'')===cur" in body
    assert "const folders=[['','🗂 All']]" in body


def test_picker_listing_bypasses_a_stale_browser_cache():
    assert "fetch(server+'/list/'+ME.pubkey,{cache:'no-store'})" in _picker()
