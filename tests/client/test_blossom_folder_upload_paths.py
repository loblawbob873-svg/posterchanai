"""A chosen OS directory must materialise as a Blossom drive folder.

This executes the production path helper in Node. A text assertion would have stayed green while
the old importer discarded the selected directory name and silently filed every upload in All.
"""

import json
import re
import subprocess
from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")
ROOT = Path(__file__).parents[2]


def test_folder_chooser_preserves_the_selected_directory_and_nested_tree():
    match = re.search(
        r"function _uploadTargetFolder\(current, relative\)\{.*?\n  \}", APP, re.S
    )
    assert match, "production folder-target helper is missing"
    cases = [
        [None, "Pictures/a.jpg"],
        ["", "Pictures/Trips/a.jpg"],
        ["Photos", "Camera/a.jpg"],
        ["Photos", "Camera/Trips/a.jpg"],
        ["Posts", "loose.jpg"],
    ]
    script = f"""
{match.group(0)}
const cases = {json.dumps(cases)};
process.stdout.write(JSON.stringify(cases.map(x => _uploadTargetFolder(x[0], x[1]))));
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == ["Pictures", "Pictures/Trips", "Photos", "Photos/Trips", "Posts"]


def test_dropped_directory_preserves_entry_paths_before_folder_routing():
    walk = re.search(r"async function _walkEntries\(entries\)\{.*?\n  \}", APP, re.S)
    target = re.search(r"function _uploadTargetFolder\(current, relative\)\{.*?\n  \}", APP, re.S)
    assert walk and target
    script = f"""
{walk.group(0)}
{target.group(0)}
const file=(path)=>({{isFile:true,fullPath:path,file(ok){{ok({{name:path.split('/').pop(),webkitRelativePath:''}})}}}});
const dir=(path, children)=>({{isDirectory:true,fullPath:path,createReader(){{let sent=false;return{{
  readEntries(ok){{if(sent)ok([]);else{{sent=true;ok(children)}}}}
}}}}}});
(async()=>{{
  const files=await _walkEntries([dir('/Pictures',[dir('/Pictures/Trips',[file('/Pictures/Trips/a.jpg')]),file('/Pictures/b.jpg')])]);
  process.stdout.write(JSON.stringify(files.map(f=>[f._pcRelativePath,_uploadTargetFolder(null,f._pcRelativePath)])));
}})();
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == [["Pictures/Trips/a.jpg", "Pictures/Trips"],
                   ["Pictures/b.jpg", "Pictures"]]


def test_folder_import_registers_every_production_target_before_uploading():
    upload = APP[APP.index("async function uploadFilesSeq(files)") :]
    upload = upload[: upload.index("// ---- Music:")]
    assert "const _subFolder=(i)=>_uploadTargetFolder(folder,_relPaths[i])" in upload
    assert "FilesIdx.addFolder(tf)" in upload
    assert "folder:_targetFolders[i]" in upload
    assert "f.webkitRelativePath||f._pcRelativePath" in upload


def test_folder_import_waits_for_index_before_deciding_encryption():
    upload = APP[APP.index("async function uploadFilesSeq(files)") :]
    upload = upload[: upload.index("// ---- Music:")]
    assert "const _importsFolder=_relPaths.some" in upload
    assert "(folder || _importsFolder) && !FilesIdx._pullDone" in upload


def test_encrypted_destination_is_decided_per_file_not_from_current_screen():
    upload = APP[APP.index("async function uploadFilesSeq(files)") :]
    upload = upload[: upload.index("// ---- Music:")]
    assert "const _targetFolders=files.map((_,i)=>_subFolder(i))" in upload
    assert "const _targetEncrypted=_targetFolders.map(tf=>!music && FilesIdx.isEncFolder(tf))" in upload
    assert "else if(_targetEncrypted[i])" in upload
    assert "uploadEncFile(files[i], _targetFolders[i], stat)" in upload


def test_encrypted_folder_rule_inherits_into_nested_import_paths():
    assert "encFolders.some(root=>name===root||name.startsWith(root+'/'))" in APP


def test_completed_folder_upload_commits_and_repaints_the_source_listing():
    """Run the production upload function, not just assertions over its source text."""
    result = subprocess.run(
        ["node", str(ROOT / "tests/client/folder_upload_completion_sim.js")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "installed folder upload completion holds" in result.stdout


def test_completed_folder_upload_refresh_is_not_deferred_to_a_timer():
    upload = APP[APP.index("async function uploadFilesSeq(files)") :]
    upload = upload[: upload.index("// ---- Music:")]
    assert "if(VIEW==='blossom') renderBlossom();" in upload
    assert "setTimeout(()=>{ if(VIEW==='blossom')" not in upload


def test_completed_upload_is_remembered_before_the_network_refresh():
    upload = APP[APP.index("async function uploadFilesSeq(files)") :]
    upload = upload[: upload.index("// ---- Music:")]
    assert "_rememberUploadedBlob(sha,url,files[i])" in upload
    helper = APP[APP.index("function _rememberUploadedBlob(") :]
    helper = helper[: helper.index("async function uploadFilesSeq(")]
    assert "_filesGridList=old.filter" in helper
    assert "_blobHave.add(sha)" in helper
    assert "_blobSizes.set(sha,row.size)" in helper


def test_folder_upload_uses_the_computed_hash_when_server_url_is_opaque():
    upload = APP[APP.index("async function uploadFilesSeq(files)") :]
    upload = upload[: upload.index("// ---- Music:")]
    assert "uploadBlob(files[i],{hashOut:stored})" in upload
    assert "const sha=stored.sha||_shaFromUrl(url)" in upload
    assert "if(!sha) throw new Error('upload completed without a content hash')" in upload


def test_folder_upload_does_not_claim_done_when_index_save_failed():
    upload = APP[APP.index("async function uploadFilesSeq(files)") :]
    upload = upload[: upload.index("// ---- Music:")]
    assert "indexSaved=!!(await FilesIdx.endBatch())" in upload
    assert "Uploaded — folder list waiting to save" in upload
    assert "indexSaved?'Done'" in upload
