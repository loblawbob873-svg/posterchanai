from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).parents[1]
CODE = (ROOT / "static/js/client/code.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
SHELL = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
# Located by GLOB, never by version: this file used to name ngit-2.6.3.ebuild, so a bump renamed
# the ebuild out from under the test and the test went red for the bump rather than for a bug.
NGIT_EBUILD = sorted((ROOT / "os/overlay/dev-vcs/ngit").glob("ngit-*.ebuild"))[-1]
NGIT = NGIT_EBUILD.read_text()


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
    assert 'RESTRICT="mirror"' in NGIT, (
        "our Gentoo mirror is an rsync of Gentoo's distfiles and has never held a file that is not "
        "in the Gentoo tree, so a mirror fetch is a guaranteed 404")


def test_posterchanos_ships_ngit_v3_or_newer():
    """v3 is the release with GRASP-08 private repositories, CI and maintainer roles — the whole
    point of this branch. Every PosterChanOS machine gets ngit as an RDEPEND of the shell, so
    shipping v2 there means the OS cannot use the features the server now implements."""
    version = NGIT_EBUILD.stem.split("-", 1)[1]
    assert int(version.split(".")[0]) >= 3, "PosterChanOS still vendors ngit %s" % version


def test_every_overlay_manifest_names_a_tarball_an_ebuild_actually_asks_for():
    """A Manifest that has drifted from its ebuild is WORSE than a missing one: the download
    succeeds and portage then rejects what it just fetched — "VERIFY FAILED! Reason: Insufficient
    data for checksum verification" — which reads as a corrupt mirror, not as a stale file. That is
    the exact way a version bump breaks, since renaming the ebuild changes ${P} and nothing else
    notices. So this checks the whole overlay, not just ngit."""
    for manifest in (ROOT / "os/overlay").rglob("Manifest"):
        pn = manifest.parent.name
        # A package NAME contains dashes too (posterchan-desktop), so the version is what is left
        # after the directory's own name — never `split("-", 1)`.
        # ...and a Gentoo REVISION is not part of the distfile name: wayfire-0.10.1-r1.ebuild
        # fetches wayfire-0.10.1.tar.xz, because ${PV} is the upstream version and ${PVR} is not.
        names = {re.sub(r"-r\d+$", "", e.stem[len(pn) + 1:])
                 for e in manifest.parent.glob("*.ebuild") if e.stem.startswith(pn + "-")}
        for line in manifest.read_text().splitlines():
            parts = line.split()
            if not parts or parts[0] != "DIST":
                continue
            assert len(parts) >= 7 and parts[2].isdigit() and int(parts[2]) > 1_000, line
            assert len(parts[4]) == 128 and len(parts[6]) == 128, (
                "BLAKE2B/SHA512 must both be present and full length: %s" % line)
            # Matched on the VERSION, not on `${P}.tar.gz`: an ebuild may keep upstream's own
            # filename (steam-launcher fetches `steam_${PV}.tar.gz` with no `->` rename), and the
            # thing that actually goes stale on a bump is the version.
            assert any(v in parts[1] for v in names), (
                "%s lists %s, but no ebuild beside it asks for that version (%s)"
                % (manifest, parts[1], sorted(names) or "no ebuilds"))


def test_git_ui_uses_json_api_not_shell_text():
    assert "post('/git/action'" in CODE
    assert "exec(" not in CODE[CODE.index("async function gitAct"):CODE.index("/* A DOCUMENT", CODE.index("async function gitAct"))]


def test_code_activity_rail_can_always_return_to_working_directory():
    assert 'data-code-view="explorer"' in CODE
    assert 'data-code-view="git"' in CODE
    assert 'aria-label="Working Directory"' in CODE
    assert "S.gitOpen=git" in CODE
    assert "if(!git)cancelGitDiff()" in CODE
    assert ".pcc-activity" in CSS


def test_working_directory_can_be_changed_on_desktop_and_browser():
    assert 'Change Working Directory' in CODE
    assert "if(h&&h.pickDirectory)" in CODE
    assert "uiPrompt('Working directory (relative to the workspace root)'" in CODE
    assert "await loadTree(String(picked).trim()" in CODE


def test_deleted_native_workspace_returns_to_folder_picker_instead_of_throwing_forever():
    assert "e.code === 'ENOENT'" in CODE
    assert "e.cause && e.cause.code === 'ENOENT'" in CODE
    assert "S.hostRoot='';S.root='No folder open';S.cwd='';S.tree=[]" in CODE
    assert "That project folder is no longer available" in CODE


def test_files_can_hand_code_a_folder_without_risking_the_current_project():
    assert "openHostFolder," in CODE[CODE.index("window.PCCode = {"):]
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/code_host_folder_sim.js")],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "code host folder runtime: ok" in run.stdout


def test_save_uses_the_same_primary_button_treatment_as_other_editor_actions():
    assert '<button class="btn btn-neon pcc-b" id="pcc-save"' in CODE


def test_desktop_git_operates_on_the_selected_local_project():
    main=(ROOT/'desktop/main.js').read_text(); preload=(ROOT/'desktop/preload.js').read_text()
    for name in ('gitStatus','gitDiff','gitAction'):
        assert name in preload and name in main
        assert f'pcHost.{name}' in CODE
    assert "S.root=S.hostRoot||'No folder open'" in CODE
    assert "if(!S.gate && (!window.pcHost || !window.pcHost.pickDirectory || S.hostRoot))" in CODE


def test_modified_file_opens_diff_in_the_editor_pane():
    assert 'data-git-diff="' in CODE
    assert "S.gitDiff={path,text:'',error:'',busy:true}" in CODE
    assert "(S.gitDiff?diffHtml():editorHtml())" in CODE
    assert 'aria-label="Diff for ' in CODE
    assert "on('#pcc-diff-close'" in CODE
    assert ".pcc-diff-view" in CSS


def test_changed_file_diff_requests_cannot_repaint_out_of_order():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/code_diff_race_sim.js")],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "code diff request ownership runtime: ok" in run.stdout


def test_each_changed_file_has_a_confirmed_discard_action():
    assert 'data-git-restore="' in CODE
    assert "Discard every change" in CODE
    assert "await gitAct('restore',[path])" in CODE


def test_discard_closes_the_visible_diff_before_the_git_refresh_repaints():
    """The restore can succeed on disk while its discarded diff remains on screen.

    ``gitAct`` ends with ``loadGit`` and therefore a repaint.  The matching diff must be cleared
    before that awaited action, otherwise no later paint reflects the cleared state.
    """
    handler = CODE[CODE.index("document.querySelectorAll('[data-git-restore]')"):]
    handler = handler[:handler.index("on('#pcc-diff-close'")]
    assert handler.index("cancelGitDiff()") < handler.index("await gitAct('restore',[path])")
    assert "grid-template-columns:minmax(0,1fr) 36px 36px" in CSS


def test_background_terminal_and_code_keep_their_full_height_layout():
    os_js = (ROOT / "static/js/client/os.js").read_text()
    app_js = (ROOT / "static/js/client/app.js").read_text()
    assert "slot.className = 'osw-slot ' + realFeed.className" in os_js
    assert "w.slot.className = 'osw-slot'" in os_js
    assert ".osw-slot.feed-term,.osw-slot.feed-code" in CSS
    assert "feed.classList.toggle('feed-code', VIEW==='code')" in app_js
