"""THE GRAPHICAL INSTALLER'S BRIDGE (desktop/installer.js), RUN under node against stub tools.

The bridge is small on purpose — gentoo.sh does the install — so what can go wrong here is exactly
the seam: offering a disk the script would refuse (or the live USB itself), putting the disk password
somewhere /proc can read it, pre-answering "erase?" when nobody confirmed an erase, and reading the
script's progress wrong. Each of those is driven here through the shipped module, with `sudo` and
`lsblk` replaced by recording stubs, and never touches a real disk.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")
GIB = 1024 ** 3

pytestmark = pytest.mark.skipif(subprocess.run(["which", "node"], capture_output=True).returncode,
                                reason="node is not installed")

LSBLK = {"blockdevices": [
    # A dd'd hybrid ISO: iso9660 on the WHOLE device, which no installed disk carries.
    {"name": "sda", "path": "/dev/sda", "type": "disk", "size": 32 * GIB, "rm": True, "ro": False,
     "tran": "usb", "model": "Flash Drive", "fstype": "iso9660", "label": "POSTERCHAN", "mountpoints": [None],
     "children": [{"name": "sda3", "path": "/dev/sda3", "type": "part", "size": 4 * GIB, "fstype": "hfsplus",
                   "label": "", "mountpoints": ["/run/initramfs/live"]}]},
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "size": 512 * GIB, "rm": False, "ro": False,
     "tran": "nvme", "model": "WD SN770", "fstype": None, "label": None, "mountpoints": [None],
     "children": [
         {"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "type": "part", "size": 100 * 1024 ** 2,
          "fstype": "vfat", "label": "SYSTEM", "mountpoints": [None]},
         {"name": "nvme0n1p2", "path": "/dev/nvme0n1p2", "type": "part", "size": 511 * GIB,
          "fstype": "ntfs", "label": "Windows", "mountpoints": [None]}]},
    {"name": "vdb", "path": "/dev/vdb", "type": "disk", "size": 64 * GIB, "rm": False, "ro": False,
     "tran": None, "model": None, "fstype": None, "label": None, "mountpoints": [None],
     "children": [
         {"name": "vdb1", "path": "/dev/vdb1", "type": "part", "size": 2 * GIB, "fstype": "vfat",
          "label": "", "mountpoints": [None]},
         {"name": "vdb2", "path": "/dev/vdb2", "type": "part", "size": 62 * GIB, "fstype": "crypto_LUKS",
          "label": "", "mountpoints": [None]}]},
    {"name": "fd0", "path": "/dev/fd0", "type": "disk", "size": 4096, "rm": True, "ro": False,
     "tran": None, "model": None, "fstype": None, "label": None, "mountpoints": [None]},
    {"name": "sr0", "path": "/dev/sr0", "type": "rom", "size": 4 * GIB, "rm": True, "ro": True,
     "tran": "sata", "model": "QEMU DVD", "fstype": "iso9660", "label": None, "mountpoints": [None]},
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "type": "disk", "size": int(7.45 * GIB), "rm": False,
     "ro": False, "tran": None, "model": "BJTD4R", "fstype": None, "label": None, "mountpoints": [None]},
]}


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "lsblk").write_text("#!/bin/sh\ncat \"$PC_TEST_LSBLK\"\n")
    (tmp_path / "lsblk.json").write_text(json.dumps(LSBLK))
    # The recording sudo: its argv and the environment it was started with, and — the thing that
    # matters — the secret file as it stood at that moment, since gentoo.sh reads it then.
    (bin_dir / "sudo").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$PC_TEST_OUT/argv\"\n"
        "env > \"$PC_TEST_OUT/environ\"\n"
        "for a in \"$@\"; do case \"$a\" in PC_INSTALL_PASSWORD_FILE=*) f=${a#*=};; esac; done\n"
        "[ -n \"$f\" ] && [ -f \"$f\" ] && { cp \"$f\" \"$PC_TEST_OUT/secret\"; stat -c %a \"$f\" > \"$PC_TEST_OUT/secret.mode\"; }\n"
        "echo '::pc-install:: medium Finding the live medium'\n"
        "exit ${PC_TEST_EXIT:-0}\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    installer = tmp_path / "gentoo.sh"
    installer.write_text("#!/bin/sh\n")
    installer.chmod(0o755)
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("BOOT_IMAGE=/boot/vmlinuz root=live:CDLABEL=POSTERCHAN rd.live.image rd.live.dir=LiveOS quiet\n")
    out = tmp_path / "out"
    out.mkdir()
    e = {**os.environ, "PC_LSBLK": str(bin_dir / "lsblk"), "PC_SUDO": str(bin_dir / "sudo"),
         "PC_TEST_LSBLK": str(tmp_path / "lsblk.json"), "PC_TEST_OUT": str(out),
         "PC_PROC_CMDLINE": str(cmdline), "PC_INSTALLER_SCRIPT": str(installer),
         "PC_INSTALLER_STATE_DIR": str(tmp_path / "state"), "PC_EFI_DIR": str(tmp_path)}
    return {"env": e, "tmp": tmp_path, "out": out, "cmdline": cmdline, "installer": installer}


def node(js, env, check=True):
    p = subprocess.run(["node", "-e", js], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    if check and p.returncode:
        raise AssertionError(p.stderr)
    return p


def call(expr, env, check=True):
    js = ("const m=require('./desktop/installer');Promise.resolve().then(()=>" + expr + ")"
          ".then(r=>process.stdout.write(JSON.stringify({ok:r})),"
          "e=>process.stdout.write(JSON.stringify({err:String(e&&e.message||e)})))")
    return json.loads(node(js, env, check).stdout)


def wait_finished(env, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = call("m.status()", env)["ok"]
        if st.get("finished") and not st.get("running"):
            return st
        time.sleep(.05)
    raise AssertionError("the supervisor never recorded a result")


# ------------------------------------------------------------------ live detection

def test_a_live_boot_with_an_installer_is_available(env):
    got = call("m.info()", env["env"])["ok"]
    assert got["live"] and got["available"] and got["installer"] == str(env["installer"])
    assert got["efi"] is True


def test_an_installed_machine_never_offers_an_installer(env):
    """No `rd.live.image` on the kernel command line is an installed system: no icon, and start()
    refuses outright even if something asked it to."""
    env["cmdline"].write_text("BOOT_IMAGE=/boot/vmlinuz root=UUID=abcd rd.luks.uuid=luks-1 quiet\n")
    got = call("m.info()", env["env"])["ok"]
    assert got == {**got, "live": False, "available": False}
    r = call("m.start({disk:'nvme0n1',password:'x',rootName:'gentoo'})", env["env"])
    assert "not a live session" in r["err"]
    assert not (env["out"] / "argv").exists()


def test_the_word_must_be_whole(env):
    env["cmdline"].write_text("root=UUID=abcd rd.live.imageX\n")
    assert call("m.info()", env["env"])["ok"]["available"] is False


# ------------------------------------------------------------------ disks

def test_the_live_medium_and_non_disks_are_never_offered(env):
    disks = {d["name"]: d for d in call("m.disks()", env["env"])["ok"]}
    assert "fd0" not in disks and "sr0" not in disks, "a floppy/optical drive was offered as a target"
    assert disks["sda"]["selectable"] is False and "running from" in disks["sda"]["why"]
    assert disks["nvme0n1"]["selectable"] is True
    assert [p["label"] for p in disks["nvme0n1"]["parts"]] == ["SYSTEM", "Windows"]
    # an "8 GB" eMMC is offered, and warned about, exactly as the terminal installer names it
    assert disks["mmcblk0"]["selectable"] is True and disks["mmcblk0"]["small"] is True


def test_an_earlier_posterchanos_layout_is_recognised_for_resume(env):
    disks = {d["name"]: d for d in call("m.disks()", env["env"])["ok"]}
    assert disks["vdb"]["posterchanLayout"] is True
    assert disks["nvme0n1"]["posterchanLayout"] is False


# ------------------------------------------------------------------ starting gentoo.sh

def test_a_fresh_install_hands_gentoo_sh_its_answers_and_never_the_password(env):
    r = call("m.start({disk:'nvme0n1',size:%d,password:'hunter2 secret',rootName:'gentoo',mode:'fresh'})"
             % (512 * GIB), env["env"])
    assert "ok" in r, r
    st = wait_finished(env["env"])
    assert st["ok"] is True and st["message"] == "PosterChanOS is installed"
    argv = (env["out"] / "argv").read_text().splitlines()
    assert argv[:2] == ["-n", "env"], "sudo must run non-interactively"
    assert argv[-2:] == [str(env["installer"]), "install-live"]
    assert "PC_INSTALL_DISK=nvme0n1" in argv and "PC_INSTALL_ROOT_NAME=gentoo" in argv
    assert "PC_INSTALL_MODE=fresh" in argv and "PC_INSTALL_PROGRESS=1" in argv
    assert "PC_ASSUME_YES=1" in argv, "a confirmed fresh install must pre-answer the erase question"
    # THE PASSWORD: in a 0600 file for the script, and in neither the argv nor the environment.
    assert "hunter2" not in "\n".join(argv)
    assert "hunter2" not in (env["out"] / "environ").read_text()
    assert (env["out"] / "secret").read_text() == "hunter2 secret"
    assert (env["out"] / "secret.mode").read_text().strip() == "600"
    # …and it does not outlive the job, even though this stub never read it.
    assert not (env["tmp"] / "state" / "installer-secret").exists()
    assert "hunter2" not in json.dumps(st)


def test_a_resume_never_carries_the_erase_answer(env):
    r = call("m.start({disk:'vdb',password:'pw',rootName:'gentoo',mode:'resume'})", env["env"])
    assert "ok" in r, r
    wait_finished(env["env"])
    argv = (env["out"] / "argv").read_text().splitlines()
    assert "PC_INSTALL_MODE=resume" in argv
    assert "PC_ASSUME_YES=1" not in argv


@pytest.mark.parametrize("expr, why", [
    ("m.start({disk:'sda',password:'pw',rootName:'gentoo'})", "running from"),
    ("m.start({disk:'sr0',password:'pw',rootName:'gentoo'})", "no longer attached"),
    ("m.start({disk:'nvme0n1',password:'pw',rootName:'../etc'})", "volume name"),
    ("m.start({disk:'nvme0n1',password:'pw',rootName:'a b'})", "volume name"),
    ("m.start({disk:'nvme0n1',password:'',rootName:'gentoo'})", "password"),
    ("m.start({disk:'nvme0n1',password:'a\\nb',rootName:'gentoo'})", "line break"),
    ("m.start({disk:'nvme0n1',size:1,password:'pw',rootName:'gentoo'})", "changed"),
    ("m.start({disk:'nvme0n1',password:'pw',rootName:'gentoo',mode:'resume'})", "to resume"),
])
def test_refusals_happen_before_anything_runs(env, expr, why):
    r = call(expr, env["env"])
    assert "err" in r and why.lower() in r["err"].lower(), r
    assert not (env["out"] / "argv").exists(), "a refused start still reached sudo"
    assert not (env["tmp"] / "state" / "installer-secret").exists()


def test_a_failed_install_is_reported_as_failed_with_its_log(env):
    env["env"]["PC_TEST_EXIT"] = "1"
    call("m.start({disk:'nvme0n1',password:'pw',rootName:'gentoo'})", env["env"])
    st = wait_finished(env["env"])
    assert st["ok"] is False and st["exitCode"] == 1
    assert "failed (exit 1)" in st["message"]
    assert "Finding the live medium" in st["log"]


def test_a_second_install_cannot_start_on_top_of_a_running_one(env):
    (env["tmp"] / "bin" / "sudo").write_text("#!/bin/sh\nsleep 2\n")
    first = call("m.start({disk:'nvme0n1',password:'pw',rootName:'gentoo'})", env["env"])
    assert "ok" in first
    second = call("m.start({disk:'nvme0n1',password:'pw',rootName:'gentoo'})", env["env"])
    assert "already running" in second.get("err", ""), second
    st = call("m.status()", env["env"])["ok"]
    assert st["running"] is True and "token" not in st


# ------------------------------------------------------------------ progress

def test_progress_follows_the_markers_and_rsyncs_percentage(env):
    log = ("::pc-install:: medium Finding the live medium\n"
           "\x1b[1;33mKernel source: /boot\x1b[0m\n"
           "::pc-install:: format Partitioning and encrypting /dev/vda\n"
           "::pc-install:: mount Opening the encrypted disk\n"
           "::pc-install:: copy Copying PosterChanOS onto the disk\n"
           "Copying the system\n"
           "    1,000,000   3%   1.00MB/s    0:00:10 (xfr#1, ir-chk=10/20)\r"
           "   40,000,000  50%   9.00MB/s    0:00:30 (xfr#9, ir-chk=1/20)\r")
    js = ("const m=require('./desktop/installer');process.stdout.write(JSON.stringify("
          "[m.progress(process.argv[1]),m.cleanLog(process.argv[1])]))")
    p = subprocess.run(["node", "-e", js, log], cwd=ROOT, capture_output=True, text=True, check=True)
    prog, clean = json.loads(p.stdout)
    assert prog["stage"] == "copy" and prog["copied"] == 50
    assert 9 < prog["percent"] < 78, "half the copy must sit half-way along the copy's share of the bar"
    assert "\x1b" not in clean and "\r" not in clean
    assert "50%" in clean and " 3%" not in clean, "a carriage-return redraw must keep only its last frame"


def test_every_answer_the_bridge_gives_is_one_gentoo_sh_reads():
    """The bridge and the script are two files; a renamed variable on either side would make the
    GUI hand over an answer nobody reads — and the script would then PROMPT, with no terminal."""
    src = (ROOT / "desktop/installer.js").read_text()
    names = set(re.findall(r"'(PC_[A-Z_]+)=", src))
    assert {"PC_INSTALL_DISK", "PC_INSTALL_ROOT_NAME", "PC_INSTALL_MODE", "PC_INSTALL_PASSWORD_FILE",
            "PC_ASSUME_YES", "PC_INSTALL_PROGRESS"} <= names
    for n in names:
        assert "${" + n in GENTOO or "$" + n in GENTOO, f"gentoo.sh never reads {n}"
    stages = set(re.findall(r"\['([a-z]+)', \d+\]", src))
    emitted = set(re.findall(r"_pc_stage ([a-z]+) ", GENTOO))
    assert stages == emitted, f"bridge stages {sorted(stages)} != gentoo.sh stages {sorted(emitted)}"


def test_an_install_longer_than_the_handoff_grace_is_still_running(env):
    """FOUND BY THE VM RUN, NOT BY THIS FILE: every stub above finishes inside the supervisor's
    2-second hand-off grace, so none of them ever asked `alive()` a real question. On a live boot the
    first real install was reported "stopped before it finished" seconds in, while gentoo.sh was
    partitioning the disk — `alive()` looks for the job's token in the supervisor's
    /proc/<pid>/cmdline, and the only copy of it there was inside the base64-encoded spec."""
    (env["tmp"] / "bin" / "sudo").write_text("#!/bin/sh\nsleep 4\nexit 0\n")
    assert "ok" in call("m.start({disk:'nvme0n1',password:'pw',rootName:'gentoo'})", env["env"])
    time.sleep(2.8)
    st = call("m.status()", env["env"])["ok"]
    assert st["running"] is True and not st["finished"], st
    assert wait_finished(env["env"])["ok"] is True
