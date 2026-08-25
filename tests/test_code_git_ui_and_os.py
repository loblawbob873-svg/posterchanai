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
