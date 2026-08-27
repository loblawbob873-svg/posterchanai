"""A chosen OS directory must materialise as a Blossom drive folder.

This executes the production path helper in Node. A text assertion would have stayed green while
the old importer discarded the selected directory name and silently filed every upload in All.
"""

import json
import re
import subprocess
from pathlib import Path


APP = (Path(__file__).parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


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


def test_folder_import_registers_every_production_target_before_uploading():
    upload = APP[APP.index("async function uploadFilesSeq(files)") :]
    upload = upload[: upload.index("// ---- Music:")]
    assert "const _subFolder=(i)=>_uploadTargetFolder(folder,_relPaths[i])" in upload
    assert "FilesIdx.addFolder(tf)" in upload
    assert "folder:_targetFolders[i]" in upload


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
