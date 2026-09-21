"""gentoo.sh install-live ANSWERED BY THE GRAPHICAL INSTALLER — run under bash, against stubs.

The GUI (desktop/installer.js) does no installing: it starts `gentoo.sh install-live` with the
answers it collected in the environment. That only works if the script (a) reads every answer instead
of prompting — a prompt with no terminal reads EOF, which is "no", which cancels the install; (b)
still puts a GUI-chosen disk through every refusal a typed one gets (the live USB, a non-disk);
(c) never trusts a /tmp/disk left by an earlier attempt over the disk the person just picked;
(d) cannot erase anything without PC_ASSUME_YES; and (e) prints its progress markers ONLY when asked,
so the terminal installer's output is exactly what it was.

The functions are extracted from the shipped os/gentoo.sh and run for real; only the commands that
touch hardware are stubs, and /tmp/disk is redirected into the test's own directory.
"""
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")
GIB = 1024 ** 3


def function(name, text=GENTOO):
    """`name() { … }` as shipped: from the definition line to the first `}` in column 0."""
    m = re.search(r"^" + re.escape(name) + r"\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, f"{name}() is gone from os/gentoo.sh — re-read this test"
    return m.group(0)


def stub_bin(tmp, disks, live_pkname="sda", live_source="/dev/sda3"):
    b = tmp / "bin"
    b.mkdir()
    rows_bd = "\n".join(f"{n} {t} {s} {m}" for n, t, s, m in disks)

    def put(name, body):
        (b / name).write_text("#!/bin/bash\n" + body + "\n")
        (b / name).chmod(0o755)

    put("lsblk", textwrap.dedent(f'''
        args="$*"
        case "$args" in *PKNAME*) echo '{live_pkname}'; exit 0;; esac
        case "$args" in *-dnro\\ TYPE*|*TYPE\\ /dev/*)
          for a in "$@"; do case "$a" in /dev/*) d=${{a#/dev/}};; esac; done
          printf '%s\\n' '{rows_bd}' | awk -v d="$d" '$1==d {{print $2}}'; exit 0;; esac
        printf '%s\\n' '{rows_bd}'
    '''))
    put("findmnt", f"echo '{live_source}'")
    put("clear", "exit 0")
    # Anything that could touch a real disk refuses loudly, so a test that reaches one fails.
    for name in ("sudo", "mount", "umount", "wipefs", "parted", "cryptsetup", "mkfs.btrfs", "mkfs.vfat"):
        put(name, f'echo "STUB-REFUSED {name} $*" >&2; exit 97')
    for name in ("partprobe", "rsync", "blkid", "mountpoint"):
        put(name, "exit 1")
    return b


def run(script, tmp, stdin=""):
    r = subprocess.run(["bash", "-c", script], input=stdin, capture_output=True, text=True, timeout=60,
                       cwd=tmp)
    return r.stdout + r.stderr, r


PRELUDE = "COLOR_YELLOW='' COLOR_RESET='' COLOR_CYAN='' COLOR_BOLD='' COLOR_RED='' COLOR_MAGENTA='' COLOR_GREEN=''\n"


# ------------------------------------------------------------------ setDevices

def set_devices_script(tmp, bin_dir, env=""):
    body = (function("setDevices")
            .replace("/tmp/disk", str(tmp / "disk"))
            # `[ -b /dev/X ]` cannot be satisfied for a name with no device node; the whole-disk half
            # of the same test (lsblk TYPE) still runs against the stub.
            .replace('[ ! -b "/dev/$device" ]', 'false'))
    return (PRELUDE + f'export PATH="{bin_dir}:$PATH"\n{env}\n'
            "partitionDetection() { HARD_DISK=$(head -1 \"" + str(tmp / "disk") + "\" 2>/dev/null); }\n"
            + body + 'setDevices; echo "RC=$? HARD_DISK=$HARD_DISK ROOT_NAME=$root_name"\n')


DISKS = [("sda", "disk", 32 * GIB, "Flash"), ("nvme0n1", "disk", 512 * GIB, "WD"), ("vdb", "disk", 64 * GIB, "-")]


def test_the_gui_disk_replaces_a_stale_hook_and_asks_nothing(tmp_path):
    b = stub_bin(tmp_path, DISKS)
    (tmp_path / "disk").write_text("nvme0n1\nold\nnone\n")        # yesterday's attempt
    out, _ = run(set_devices_script(tmp_path, b, "export PC_INSTALL_DISK=vdb PC_INSTALL_ROOT_NAME=pcos"),
                 tmp_path, stdin="")
    assert (tmp_path / "disk").read_text().split() == ["vdb", "pcos", "none"], out
    assert "Disk Device to Use:" in out and "graphical installer" in out
    # No prompt was issued at all — with no terminal each one reads EOF.
    assert "BTRFS Root Volume name" not in out
    assert "Disk Device to Use [" not in out


def test_the_gui_cannot_name_the_live_usb(tmp_path):
    b = stub_bin(tmp_path, DISKS, live_pkname="sda")
    out, _ = run(set_devices_script(tmp_path, b, "export PC_INSTALL_DISK=sda"), tmp_path)
    assert "Refusing to install onto the live boot disk /dev/sda" in out
    assert not (tmp_path / "disk").exists()


def test_the_gui_cannot_name_something_that_is_not_a_whole_disk(tmp_path):
    b = stub_bin(tmp_path, DISKS + [("sr0", "rom", 4 * GIB, "DVD")])
    out, _ = run(set_devices_script(tmp_path, b, "export PC_INSTALL_DISK=sr0"), tmp_path)
    assert "Not a whole disk: /dev/sr0" in out
    assert not (tmp_path / "disk").exists()


@pytest.mark.parametrize("bad", ["../etc", "a b", "x,subvol=@home", "-rf", "a" * 40])
def test_a_volume_name_handed_in_is_held_to_a_safe_shape(tmp_path, bad):
    b = stub_bin(tmp_path, DISKS)
    out, _ = run(set_devices_script(tmp_path, b, f"export PC_INSTALL_DISK=vdb PC_INSTALL_ROOT_NAME='{bad}'"),
                 tmp_path)
    assert "Not a usable volume name" in out
    assert not (tmp_path / "disk").exists()


def test_the_terminal_path_still_prompts_and_still_trusts_its_hook(tmp_path):
    """Unchanged without the GUI's variables: a /tmp/disk is read, and with none, the prompts ask."""
    b = stub_bin(tmp_path, DISKS)
    (tmp_path / "disk").write_text("nvme0n1\ngentoo\nnone\n")
    out, _ = run(set_devices_script(tmp_path, b), tmp_path)
    assert "RC=0 HARD_DISK=nvme0n1" in out
    (tmp_path / "disk").unlink()
    out, _ = run(set_devices_script(tmp_path, b).replace('read -r -p "Disk Device to Use [$DEFAULT_DISK]: " device',
                                                         'read -r device'), tmp_path, stdin="vdb\nmine\n")
    assert (tmp_path / "disk").read_text().split() == ["vdb", "mine", "none"], out


# ------------------------------------------------------------------ the password

def test_the_password_file_is_read_once_and_removed(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("correct horse")                 # no trailing newline — as the bridge writes it
    script = (PRELUDE + function("readInstallPassword")
              + f'export PC_INSTALL_PASSWORD_FILE="{secret}"\n'
              + 'readInstallPassword confirm; echo "RC=$? DISK=[$DISK_PASSWORD] ROOT=[$ROOT_PASSWORD]"\n')
    out, _ = run(script, tmp_path, stdin="")
    assert "RC=0 DISK=[correct horse] ROOT=[correct horse]" in out, out
    assert not secret.exists(), "the password file outlived its reading"
    assert "Confirm password" not in out and "Disk encryption and recovery password" not in out


def test_a_missing_password_file_is_a_refusal_not_a_blank_password(tmp_path):
    script = (PRELUDE + function("readInstallPassword")
              + f'export PC_INSTALL_PASSWORD_FILE="{tmp_path}/nope"\n'
              + 'readInstallPassword confirm </dev/null; echo "RC=$?"\n')
    out, _ = run(script, tmp_path)
    assert "RC=1" in out and "blank encryption password is not allowed" in out


# ------------------------------------------------------------------ the erase question

def live_install_script(tmp, bin_dir, env=""):
    boot = tmp / "boot"
    boot.mkdir(exist_ok=True)
    (boot / "vmlinuz").write_text("k")
    body = function("liveISOinstall").replace("ls -A /boot", f'ls -A "{boot}"')
    return (PRELUDE + f'export PATH="{bin_dir}:$PATH"\n{env}\n'
            + function("_pc_stage")
            + 'setDevices() { HARD_DISK=vdb; EFI=/nonexistent1; BTRFS=/nonexistent2; return 0; }\n'
            + 'readInstallPassword() { echo "PASSWORD-READ"; return 0; }\n'
            + 'prepareInstallDisk() { echo "PREPARED /dev/$HARD_DISK"; return 1; }\n'
            + body + "liveISOinstall; echo \"RC=$?\"\n")


def test_without_assume_yes_nothing_is_erased(tmp_path):
    b = stub_bin(tmp_path, DISKS)
    out, _ = run(live_install_script(tmp_path, b, "export PC_INSTALL_DISK=vdb"), tmp_path, stdin="")
    assert "Install cancelled; nothing was written." in out
    assert "PREPARED" not in out and "PASSWORD-READ" not in out


def test_the_gui_confirmation_reaches_the_disk_step_and_reports_progress(tmp_path):
    b = stub_bin(tmp_path, DISKS)
    out, _ = run(live_install_script(tmp_path, b, "export PC_INSTALL_DISK=vdb PC_ASSUME_YES=1 PC_INSTALL_PROGRESS=1"),
                 tmp_path, stdin="")
    assert "PREPARED /dev/vdb" in out, out
    markers = re.findall(r"^::pc-install:: (\S+)", out, re.M)
    assert markers == ["medium", "disk", "format"], markers
    assert "Erase /dev/vdb and install PosterChanOS? yes" in out


def test_the_terminal_install_prints_exactly_what_it_did(tmp_path):
    """No GUI variables: the prompt is asked and answered from stdin, and no marker line appears."""
    b = stub_bin(tmp_path, DISKS)
    out, _ = run(live_install_script(tmp_path, b), tmp_path, stdin="y\n")
    assert "PREPARED /dev/vdb" in out
    assert "::pc-install::" not in out
