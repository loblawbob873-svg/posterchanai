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
import re
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


def _run_main(tmp_path, extra, monkeypatch):
    """Drive the gate's real main() with the install itself replaced by a recorder."""
    seen = {}

    def fake_install(iso, disk, serial_dir, evidence, timeout, memory, cpus, usb=False):
        seen["usb"] = usb
        return 0

    monkeypatch.setattr(MOD, "install", fake_install)

    # THE BOOT PHASE IS RECORDED, NOT MERELY SILENCED. main() now installs and then boots the disk
    # with no ISO; stubbing that without checking it happened would let a main() that skipped booting
    # pass — which is precisely the defect this file exists around (the gate promised for months that
    # it booted the installed disk and never did).
    def fake_boot(disk, serial_dir, vars_copy, evidence, timeout, memory, cpus, **kw):
        seen["booted"] = True
        seen["boot_vars"] = str(vars_copy)
        return 0

    monkeypatch.setattr(MOD, "boot_installed", fake_boot)
    # `main()` refuses to start without qemu on the host, and returns 2 — "could not run" — long
    # before it reaches `install()`. This test is about ARGUMENT PLUMBING (does `--usb` arrive at
    # install?), which needs no qemu at all, so on a host without it the test was failing `2 == 0`
    # about a flag it had not looked at. Skipping the whole test instead would have retired the
    # coverage on every machine that does not build ISOs, which is most of them — including this
    # one. Satisfy the precondition it does not depend on, and keep measuring the thing it does.
    _real_which = MOD.shutil.which
    monkeypatch.setattr(MOD.shutil, "which",
                        lambda tool: "/usr/bin/" + tool if tool.startswith("qemu-")
                        else _real_which(tool))
    # ...and `main()` then creates the disk with a real `qemu-img`. Record the call rather than
    # swallowing it: a `main()` that stopped making a disk at all would otherwise pass this test.
    made = []
    _real_run = MOD.subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "qemu-img":
            made.append(cmd)
            return __import__("subprocess").CompletedProcess(cmd, 0)
        return _real_run(cmd, *a, **kw)

    monkeypatch.setattr(MOD.subprocess, "run", fake_run)
    iso = tmp_path / "fake.iso"
    iso.write_bytes(b"not really an iso")
    monkeypatch.setattr(sys, "argv", ["check_livecd_install_vm.py", str(iso),
                                      "--disk", str(tmp_path / "d.qcow2"),
                                      "--size", "64M",
                                      "--evidence-dir", str(tmp_path / "ev")] + extra)
    assert MOD.main() == 0
    assert made and made[0][:2] == ["qemu-img", "create"], (
        "main() no longer creates the guest disk — this gate would boot against nothing")
    return seen


def test_asking_for_a_usb_actually_boots_a_usb(tmp_path, monkeypatch):
    """--usb REACHES the guest, and the flag is worth nothing if it does not.

    `qemu_args(usb=True)` was correct and `main()` never passed `args.usb` to `install()`, so every
    run of this gate booted the ISO as a CD-ROM — the one medium on which the installer's kernel
    search cannot fail, and the exact reason the flag was added. A silently ignored flag makes the
    gate report a pass about a medium it did not test.
    """
    assert _run_main(tmp_path, ["--usb"], monkeypatch)["usb"] is True
    assert _run_main(tmp_path, [], monkeypatch)["usb"] is False


def test_the_usb_guest_is_a_partitioned_stick_not_a_cdrom(tmp_path):
    """The medium's SHAPE is the point: a stick is a partitioned disk behind a protective MBR, a
    CD-ROM is an iso9660 /dev/sr0. They reach liveISOinstall's kernel search by different paths."""
    stick = " ".join(MOD.qemu_args("d.qcow2", "x.iso", "s.sock", None, None, 4096, 4, True))
    disc = " ".join(MOD.qemu_args("d.qcow2", "x.iso", "s.sock", None, None, 4096, 4, False))
    assert "usb-storage" in stick and "media=cdrom" not in stick
    assert "media=cdrom" in disc and "usb-storage" not in disc


def test_the_usb_stick_cannot_rewrite_the_image_under_test(tmp_path):
    """The guest gets a writable stick; the ISO file itself must stay untouched.

    Without snapshot=on qemu opens the ISO read-WRITE, so the guest can alter the artifact the
    run is meant to certify — and an ISO built under sudo is root-owned 644, which makes that
    open fail outright with EACCES. The gate then died on a bare ConnectionRefusedError from the
    console socket, naming the socket instead of the permission denial qemu had printed.
    """
    stick = " ".join(MOD.qemu_args("d.qcow2", "x.iso", "s.sock", None, None, 4096, 4, True))
    drive = next(a for a in stick.split() if a.startswith("file=x.iso"))
    assert "snapshot=on" in drive, drive
    assert "readonly=on" not in drive, "the guest must still SEE a writable stick"


def test_a_dead_qemu_is_reported_not_raised():
    """A -drive qemu rejects leaves the socket path present and nothing listening."""
    assert SRC.count("proc.poll() is not None") >= 2, (
        "only the socket-absent case checks whether qemu is still alive")
    assert "could not attach to the guest console" in SRC


def test_main_boots_the_disk_it_installed(tmp_path, monkeypatch):
    """The promise in this module's docstring, pinned.

    `check_livecd_install_vm.py` opened with "Install PosterChanOS from an ISO into a blank virtual
    disk, then boot that disk with no ISO" and "Exit 0 installed and the installed disk booted", and
    `main()` did the first half only: it installed, printed OK, deleted the disk and returned 0. The
    one gate whose stated purpose was proving an installed system boots had never booted one, so
    "installer failed to make a bootable system" reached a user with every gate green.
    """
    seen = _run_main(tmp_path, [], monkeypatch)
    assert seen.get("booted") is True, (
        "main() installed and never booted the result — the whole point of this gate")


def test_the_boot_reuses_the_nvram_the_install_wrote(tmp_path, monkeypatch):
    """A FRESH variables file would test the removable-media fallback and call it a pass.

    The EFI boot entry the installer creates lives in OVMF_VARS.fd. Handing the boot phase a clean
    copy means the guest has no entries at all, so its firmware falls back to EFI/BOOT/BOOTX64.EFI
    and boots — which is exactly how an install with no boot variable looked healthy in every VM
    while being unbootable on a machine that had previously run Windows.
    """
    seen = _run_main(tmp_path, [], monkeypatch)
    assert seen.get("boot_vars", "").endswith("OVMF_VARS.fd"), seen


def test_the_boot_check_can_actually_hear_the_installed_system():
    """A SILENT PASS AND A SILENT FAILURE LOOK IDENTICAL, AND THIS ONE LOOKED LIKE A FAILURE.

    The LIVE image carries `console=ttyS0,115200n8` from the ISO's own grub line. The INSTALLED
    system does not — its loader entry is `quiet splash … root=UUID=… rw rd.luks.uuid=…`. So the
    firmware and systemd-boot write to the serial port, the kernel takes over, and everything after
    that goes to the graphical console.

    Measured 2026-09-18: the boot console stopped after 1137 bytes at systemd-boot's menu and stayed
    silent for the full timeout. The gate reported "printed nothing recognisable", about an ISO that
    booted perfectly — a deaf harness reporting a broken product. Re-run with a serial console added
    to the test's copy of the entry, the same disk reached a running system in seconds.

    `quiet` is dropped for the same reason: it suppresses the very messages being waited for. This
    edits the TEST's copy of the disk, never the ISO, and changes nothing that decides whether the
    system boots.
    """
    from pathlib import Path as _Path
    src = (_Path(__file__).resolve().parents[1] / "scripts/check_livecd_install_vm.py").read_text()
    assert "_make_installed_boot_audible" in src, (
        "the boot check no longer gives the installed system a console it can read")
    fn = src[src.index("def _make_installed_boot_audible"):]
    fn = fn[:fn.index("\ndef ", 1)]
    assert "console=ttyS0" in fn, "no serial console is added, so the boot phase reads nothing"
    assert 'replace(" quiet", "")' in fn, "`quiet` is left on, which suppresses what is waited for"
    # It must edit the DISK, not the ISO: the artifact under test stays untouched.
    assert ".iso" not in fn, "the boot check is editing the ISO rather than its installed copy"
    called = src[src.index("def boot_installed("):]
    called = called[:called.index("\ndef ", 1)] if "\ndef " in called[1:] else called
    assert "_make_installed_boot_audible(" in called, "the helper exists but is never called"


# ---- the server, switched on FROM the installed system (--server)

class _FakeConsole:
    """The installed guest's serial console: every line sent is ECHOED (as a real tty does -- which is
    what proves a marker cannot be matched from the command text itself) and answered by the first
    rule whose pattern matches the command."""
    def __init__(self, rules):
        self.buf = "posterchanos login: "
        self.rules = rules
        self.sent = []

    def send(self, line):
        self.sent.append(line)
        self.buf += line + "\n"
        for pattern, answer in self.rules:
            if re.search(pattern, line):
                out = answer() if callable(answer) else answer
                if out:
                    self.buf += out + "\n"
                break

    def expect(self, pattern, timeout, since=0):
        m = re.compile(pattern).search(self.buf, since)
        return m.end() if m else None


def _stage(rules, **kw):
    ticks = iter(range(0, 10 ** 6, 5))
    con = _FakeConsole(rules)
    rc = MOD.server_stage(con, "pc-vm-test-only", kw.pop("timeout", 600), "/tmp/ev",
                          poll=0, clock=lambda: next(ticks), sleep=lambda s: None)
    return rc, con


def _answers(job_rc="0", posts=(0, 3, 12), code=True):
    jobs = iter(['PCJOB| {"running":true,"verb":"enable","rc":""}'])
    counts = iter(posts)
    return [
        (r"^root$", "Password: "),
        (r"^pc-vm-test-only$", "root@posterchanos:~#"),
        (r"ROOT-\$\(id -u\)", "ROOT-0"),
        (r"pc-server status", 'PCSTATUS| {"code":%s,"configured":false}' % ("true" if code else "false")),
        (r"pc-server enable", "started: enable\nENABLE-RC=0"),
        (r"pc-server job \|", lambda: next(jobs, 'PCJOB| {"running":false,"verb":"enable","rc":"%s"}' % job_rc)),
        (r"job-log", "JOBLOG| pip failed\nJOBLOG-END"),
        (r"SVC=", "SVC=active HTTP=200"),
        (r"POSTS=", lambda: "POSTS=%d" % next(counts, posts[-1])),
        (r"journalctl", "SVCLOG| no upstream\nSVCLOG-END"),
    ]


def test_the_server_passes_only_once_nostr_posts_are_seen_arriving():
    rc, con = _stage(_answers())
    assert rc == 0, con.buf
    assert sum("POSTS=" in s for s in con.sent) >= 2, "it passed before posts were counted"
    assert any("pc-server enable" in s for s in con.sent)


def test_a_failed_server_install_fails_the_gate_with_its_log():
    rc, con = _stage(_answers(job_rc="1"))
    assert rc == 1
    assert any("job-log" in s for s in con.sent), "the job's own log was not captured"


def test_a_server_that_runs_but_receives_no_posts_fails():
    """Active and answering on 3051 is not the pass condition: a node whose relay syncs nothing is
    useless, and that is the failure the user asked this gate to catch."""
    rc, con = _stage(_answers(posts=(0,)), timeout=600)
    assert rc == 1, con.buf
    assert any("journalctl" in s for s in con.sent)


def test_an_image_without_the_server_code_fails_before_enabling_anything():
    rc, con = _stage(_answers(code=False))
    assert rc == 1
    assert not any("pc-server enable" in s for s in con.sent)


def test_with_server_the_installed_guest_gets_a_network():
    joined = " ".join(MOD.qemu_args("/d", None, "/s", None, None, 1, 1, net=True))
    assert "-nic user,model=virtio-net-pci" in joined
    assert "-nic" not in " ".join(MOD.qemu_args("/d", "/x.iso", "/s", None, None, 1, 1))
