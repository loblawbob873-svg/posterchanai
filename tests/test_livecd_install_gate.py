"""THE INSTALLER ITSELF HAD NO GATE, and that is a different thing from the ISO booting.

`check_livecd_vm.py` proves an image reaches a graphical session. It says nothing about whether the
installer on it works — and an image that boots and cannot install is the whole product missing.
Every install before this was done by hand, which is why `check_installed_vm.py` asks to be handed a
domain that "already contains an installed system".

These run the gate's own logic; the end-to-end install is the gate, run against a real ISO.
"""
from pathlib import Path
import ast
import importlib.util
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts/check_livecd_install_vm.py"
SRC = GATE.read_text(encoding="utf-8")
SPEC = importlib.util.spec_from_file_location("livecd_install_vm", GATE)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_failed_installer_cannot_pass_because_printf_succeeded(tmp_path):
    commands = [node.args[0].value for node in ast.walk(ast.parse(SRC))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send" and node.args
                and isinstance(node.args[0], ast.Constant)]
    command = next(value for value in commands if "sudo gentoo.sh install-live" in value)
    command = command.replace("sudo gentoo.sh install-live", "bash -c 'cat >/dev/null; exit 17'")
    command = command.replace("/tmp/pc-install-test.log", str(tmp_path / "install.log"))
    got = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert "INSTALL-EXIT-17" in got.stdout


def test_no_iso_is_a_skip_that_says_nothing_was_verified():
    """Exit 2 is "could not run" and the suite reports it as a SKIP. A gate that exits 0 with no
    image would be a green tick for an installer nobody tested."""
    got = subprocess.run([sys.executable, str(GATE)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    assert got.returncode == 2
    assert "Nothing was verified" in got.stdout


def test_the_guest_is_uefi_because_that_is_what_the_installer_writes():
    """The installer writes an ESP and a systemd-boot entry. A SeaBIOS guest would boot the disk
    through a path the product never uses and prove nothing about the bootloader."""
    code, vars_ = MOD.ovmf()
    args = MOD.qemu_args("/tmp/d.qcow2", "/tmp/x.iso", "/tmp/s.sock", code or "/c.fd",
                         "/tmp/v.fd", 4096, 4)
    joined = " ".join(args)
    assert "if=pflash" in joined, joined
    assert "unit=0,readonly=on" in joined
    # The variable store is per run: firmware WRITES its boot entries there, so a shared file makes
    # the second run's result depend on the first.
    assert "unit=1,file=/tmp/v.fd" in joined
    assert "OVMF_VARS.fd" in SRC and "shutil.copyfile(vars_src, vars_copy)" in SRC


def test_it_boots_the_installer_medium_and_can_boot_without_one():
    with_iso = " ".join(MOD.qemu_args("/tmp/d.qcow2", "/tmp/x.iso", "/tmp/s.sock", None, None,
                                      4096, 4))
    assert "media=cdrom" in with_iso and "-boot order=d" in with_iso
    # The second half of the gate boots the INSTALLED disk with no installer attached — an ISO left
    # in the drive would boot the live image again and pass while the bootloader was broken.
    without = " ".join(MOD.qemu_args("/tmp/d.qcow2", None, "/tmp/s.sock", None, None, 4096, 4))
    assert "media=cdrom" not in without
    assert "if=virtio" in without


def test_the_install_is_driven_over_the_serial_console_not_the_gui():
    """Sending synthetic keystrokes at a desktop tests QEMU's keymap. The ISO's kernel command line
    already carries console=ttyS0, so the gate types the same commands a person would."""
    assert "-serial" in " ".join(MOD.qemu_args("/d", None, "/s", None, None, 1, 1))
    assert "/tmp/disk" in SRC, "the installer's own scripting hook is what avoids the prompts"
    # The values the installer's INTERACTIVE path writes when its prompts are answered with their
    # defaults. Invented ones would exercise a configuration nobody ships.
    installer = (ROOT / "os/gentoo.sh").read_text()
    assert "echo none >>/tmp/disk" in installer and "root_name:-gentoo" in installer
    assert r"gentoo\\nnone" in SRC
    assert "install-live" in SRC
    assert "INSTALL-EXIT-" in SRC, "the installer's exit status is read, not assumed"


def test_a_login_prompt_is_reported_as_the_missing_autologin_it_is():
    """`live` is password-LOCKED on purpose, so a serial LOGIN prompt is unanswerable. Without the
    serial-getty override in the image this gate would otherwise time out saying only that the
    installer never finished."""
    assert "live@[-a-z0-9]+" in SRC
    assert "serial-getty autologin override is missing" in SRC
    installer = (ROOT / "os/gentoo.sh").read_text()
    assert "serial-getty@ttyS0.service.d" in installer
    assert "agetty --autologin live --keep-baud" in installer


def test_a_blank_disk_every_run():
    """Installing over a previous install exercises the resume path, which is the one that does not
    erase. The fresh path is what a new machine gets."""
    assert "if disk.exists():" in SRC and "disk.unlink()" in SRC
    assert 'qemu-img", "create"' in SRC


def test_the_transcript_survives_a_failure():
    """"The install did not finish" with no console output cannot be acted on."""
    assert "install-console.log" in SRC
    assert SRC.count("transcript in") >= 2
