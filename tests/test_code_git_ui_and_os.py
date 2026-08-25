from pathlib import Path


ROOT = Path(__file__).parents[1]
CODE = (ROOT / "static/js/client/code.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
SHELL = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
NGIT = (ROOT / "os/overlay/dev-vcs/ngit/ngit-2.6.3.ebuild").read_text()


def test_code_has_a_complete_source_control_surface():
    for text in ("Source Control", "Pull", "Push", "Commit message", "data-git-diff",
                 "action, paths", "'/git/status'", "'/git/action'"):
        assert text in CODE
    assert ".pcc-git-file" in CSS and ".pcc-git-diff" in CSS


def test_posterchanos_installs_ngit_and_the_git_remote_helper_automatically():
    assert "dev-vcs/ngit" in SHELL
    assert "dobin ngit git-remote-nostr" in NGIT
    assert "x86_64-unknown-linux-gnu.2.17" in NGIT
    assert "-> ${P}.tar.gz" in NGIT
    manifest = (ROOT / "os/overlay/dev-vcs/ngit/Manifest").read_text().split()
    assert manifest[:2] == ["DIST", "ngit-2.6.3.tar.gz"]
    assert int(manifest[2]) > 1_000_000
    assert len(manifest[4]) == 128 and len(manifest[6]) == 128


def test_git_ui_uses_json_api_not_shell_text():
    assert "post('/git/action'" in CODE
    assert "exec(" not in CODE[CODE.index("async function gitAct"):CODE.index("/* A DOCUMENT", CODE.index("async function gitAct"))]


def test_code_activity_rail_can_always_return_to_working_directory():
    assert 'data-code-view="explorer"' in CODE
    assert 'data-code-view="git"' in CODE
    assert 'aria-label="Working Directory"' in CODE
    assert "S.gitOpen=git" in CODE
    assert "if(!git)S.gitDiff=null" in CODE
    assert ".pcc-activity" in CSS


def test_working_directory_can_be_changed_on_desktop_and_browser():
    assert 'Change Working Directory' in CODE
    assert "if(h&&h.pickDirectory)" in CODE
    assert "uiPrompt('Working directory (relative to the workspace root)'" in CODE
    assert "await loadTree(String(picked).trim()" in CODE


def test_modified_file_opens_diff_in_the_editor_pane():
    assert 'data-git-diff="' in CODE
    assert "S.gitDiff={path,text:'',error:'',busy:true}" in CODE
    assert "(S.gitDiff?diffHtml():editorHtml())" in CODE
    assert 'aria-label="Diff for ' in CODE
    assert "on('#pcc-diff-close'" in CODE
    assert ".pcc-diff-view" in CSS


def test_each_changed_file_has_a_confirmed_discard_action():
    assert 'data-git-restore="' in CODE
    assert "Discard every change" in CODE
    assert "await gitAct('restore',[path])" in CODE
    assert "grid-template-columns:minmax(0,1fr) 36px 36px" in CSS


def test_background_terminal_and_code_keep_their_full_height_layout():
    os_js = (ROOT / "static/js/client/os.js").read_text()
    assert "slot.className = 'osw-slot ' + realFeed.className" in os_js
    assert "w.slot.className = 'osw-slot'" in os_js
    assert ".osw-slot.feed-term,.osw-slot.feed-code" in CSS
