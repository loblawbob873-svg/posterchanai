"""The graphical installer's own rules (static/js/client/osinstall.js) and its place on the desktop.

`PCInstaller._pure` is the wizard's decision half — which step may continue and why not, how the
script's phases map onto the checklist — run here under node. The desktop wiring is checked from the
shipped os.js/oswin.js/app.js/preload.js text for the four places a desktop-built screen has to be
registered (a name missing from any one of them opens an empty or wrongly-titled window, which is
how that family of bug has always read from the outside). scripts/check_os_installer.py drives the
real screens in a browser.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "static/js/client/osinstall.js"
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(subprocess.run(["which", "node"], capture_output=True).returncode,
                                reason="node is not installed")


def pure(expr):
    js = (f"globalThis.window=globalThis;require({json.dumps(str(UI))});const P=window.PCInstaller._pure;"
          f"process.stdout.write(JSON.stringify({expr}))")
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


DISKS = ("[{name:'sda',selectable:false,why:'This is the drive PosterChanOS is running from.'},"
         "{name:'nvme0n1',selectable:true,why:'',posterchanLayout:false},"
         "{name:'vdb',selectable:true,why:'',posterchanLayout:true}]")


def test_no_disk_no_next_and_it_says_so():
    assert "Choose the disk" in pure(f"P.blocker('disk',{{disks:{DISKS},disk:''}})")


def test_the_live_usb_cannot_be_carried_forward_even_if_selected():
    assert "running from" in pure(f"P.blocker('disk',{{disks:{DISKS},disk:'sda'}})")


def test_resume_needs_an_earlier_install():
    assert "resume" in pure(f"P.blocker('disk',{{disks:{DISKS},disk:'nvme0n1',mode:'resume'}})")
    assert pure(f"P.blocker('disk',{{disks:{DISKS},disk:'vdb',mode:'resume'}})") == ""


@pytest.mark.parametrize("state, why", [
    ("{password:'',password2:'',rootName:'gentoo'}", "Choose an encryption password"),
    ("{password:'a',password2:'b',rootName:'gentoo'}", "not the same"),
    ("{password:'a\\nb',password2:'a\\nb',rootName:'gentoo'}", "line break"),
    ("{password:'a',password2:'a',rootName:'../x'}", "volume name"),
    ("{password:'a',password2:'a',rootName:''}", "volume name"),
])
def test_the_password_step_refuses(state, why):
    assert why in pure(f"P.blocker('security',{state})")


def test_matching_passwords_pass_and_a_resume_needs_no_confirmation():
    assert pure("P.blocker('security',{password:'a',password2:'a',rootName:'gentoo'})") == ""
    assert pure("P.blocker('security',{password:'a',password2:'',rootName:'gentoo',mode:'resume'})") == ""


def test_erase_needs_the_disks_exact_name_typed():
    base = f"disks:{DISKS},disk:'nvme0n1'"
    assert "Type the disk" in pure(f"P.blocker('summary',{{{base},typed:''}})")
    assert "Type the disk" in pure(f"P.blocker('summary',{{{base},typed:'nvme0n'}})")
    assert "Type the disk" in pure(f"P.blocker('summary',{{{base},typed:'NVME0N1'}})")
    assert pure(f"P.blocker('summary',{{{base},typed:'nvme0n1'}})") == ""
    # a resume erases nothing, so there is nothing to type
    assert pure(f"P.blocker('summary',{{disks:{DISKS},disk:'vdb',mode:'resume',typed:''}})") == ""


def test_the_checklist_follows_the_scripts_stages():
    got = pure("P.phaseStates({running:true,progress:{stage:'bootloader'}}).map(p=>p.state)")
    assert got == ["done", "done", "active", "todo", "todo"]
    got = pure("P.phaseStates({finished:1,ok:false,progress:{stage:'copy'}}).map(p=>p.state)")
    assert got == ["done", "failed", "todo", "todo", "todo"]
    got = pure("P.phaseStates({finished:1,ok:true,progress:{stage:'verify'}}).map(p=>p.state)")
    assert got == ["done"] * 5
    # a failure before the first marker still marks the first phase, never a silent row of todos
    assert pure("P.phaseStates({finished:1,ok:false,progress:{stage:''}})[0].state") == "failed"


def test_every_stage_gentoo_sh_emits_belongs_to_a_phase():
    stages = set(re.findall(r"_pc_stage ([a-z]+) ", (ROOT / "os/gentoo.sh").read_text()))
    grouped = set(pure("[].concat(...P.PHASES.map(p=>p.stages))"))
    assert stages <= grouped, f"stages with no row on the checklist: {sorted(stages - grouped)}"


def test_the_failure_tail_leaves_out_the_markers():
    assert pure("P.tailLines('::pc-install:: copy x\\nrsync: no space\\n\\nThe copy did not complete',5)") \
        == "rsync: no space\nThe copy did not complete"


# ------------------------------------------------------------------ the desktop wiring

def test_the_installer_is_an_extra_that_leads_the_desktop_only_on_a_live_boot():
    block = OS_JS.split("const EXTRAS = [", 1)[1].split("\n  ];", 1)[0]
    entry = block.split("view: '__installer'", 1)[1].split("},\n", 1)[0]
    assert "first: true" in entry
    assert "pcInstaller.isLive" in entry, "the icon must be gated on the live-boot answer"
    assert "return extras.filter(x => x.first).concat(sidebar" in OS_JS


@pytest.mark.parametrize("where, needle", [
    ("static/js/client/os.js", "'__installer':     { view: '__installer'"),                  # EXTRA_WINDOWS
    ("static/js/client/os.js", "'__installer':  () => _loadInstaller()"),                     # EXTRA_RENDER
    ("static/js/client/oswin.js", "'__installer'"),                                           # routable
    ("static/js/client/app.js", "__installer:'Install PosterChanOS'"),                       # heading
    ("desktop/preload.js", "exposeInMainWorld('pcInstaller'"),
    ("desktop/main.js", "ipcMain.handle('pc:installer:start'"),
])
def test_registered_everywhere_a_desktop_screen_has_to_be(where, needle):
    assert needle in (ROOT / where).read_text(encoding="utf-8"), f"{needle!r} missing from {where}"


def test_the_page_is_loaded_on_demand_not_by_every_client():
    """Only a live USB ever opens it: a page tag would put it on every cold load, everywhere."""
    assert "osinstall.js" not in (ROOT / "templates/client.html").read_text()
    assert "'/static/js/client/osinstall.js'" in OS_JS


def test_no_native_dialog_in_the_installer():
    src = re.sub(r"/\*.*?\*/", "", UI.read_text(), flags=re.S)      # comments may name them
    assert not re.search(r"(?<![\w.])(confirm|alert|prompt)\s*\(", src.replace("uiConfirm(", "")), \
        "a native dialog wedges the Electron renderer's focus — use PC().uiConfirm"
