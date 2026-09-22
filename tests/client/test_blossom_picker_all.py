from pathlib import Path
from tests.client_source import client_source


ROOT = Path(__file__).resolve().parents[2]
APP = client_source()


def _picker():
    start = APP.index("function blossomPicker(ta, onPick, opts={})")
    return APP[start:APP.index("\n  // ---------- Pics:", start)]


def test_all_files_builds_urls_for_standard_blossom_list_entries():
    body = _picker()
    assert "if(Array.isArray(rows))" in body
    assert "rows.filter(b=>b&&b.sha256).map" in body
    assert "url:b.url || (server.replace(/\\/$/,'')+'/'+b.sha256)" in body


def test_all_files_does_not_filter_to_only_root_folder():
    """THE RULE, NOT THE CALL TEXT.

    This pinned the literal `cur==='' || (FilesIdx.folderOf(b.sha256)||'')===cur`, which stopped
    matching when folder rows moved out of the bounded listing window and into the index — a change
    made because that filter was showing 0 of 79 files in a folder. What must hold is that "All"
    (cur === '') is NOT narrowed to one folder, and that a named folder IS matched on its own name.
    """
    body = _picker()
    rows = body[body.index("const _folderRows"):]
    rows = rows[:rows.index("\n      };") + 1]
    assert "if(!f) return list;" in rows, \
        "All files no longer means the whole list — it is being filtered to a folder"
    assert "FilesIdx.folderOf" in rows, \
        "a named folder is no longer matched by the file's own folder"
    assert "=== f" in rows or "===f" in rows, \
        "the folder comparison is gone"
    assert "const folders=[['','🗂 All']]" in body


# The picker now asks for a BOUNDED page (`?limit=`) with an abort deadline, so the call is no
# longer one literal string. What must not change is that it refuses a stale browser copy — pin
# THAT, the way tests here are meant to pin rules rather than call text.
def test_picker_listing_bypasses_a_stale_browser_cache():
    assert "cache:'no-store'" in _picker()


def test_all_listing_does_not_wait_forever_for_folder_index_hydration():
    body = _picker()
    assert body.index("const listing=(async()=>") < body.index("FilesIdx.ensure()")
    assert "Promise.race([FilesIdx.ensure(),new Promise(resolve=>setTimeout(resolve,4000))])" in body
    assert "const rows=await listing" in body
    assert "Array.isArray(body&&body.blobs)?body.blobs" in body


def test_picker_has_date_name_size_sort_and_date_metadata():
    body = _picker()
    assert 'class="bp-sort"' in body
    assert '<option value="date">Newest</option>' in body
    assert '<option value="name">Name</option>' in body
    assert '<option value="size">Size</option>' in body
    assert "Number(b.uploaded||b.created_at)" in body
    assert 'class="bp-pick-date"' in body
