"""`gentoo.sh anticheat` / `anticheat-off`: the opt-in Easy-Anti-Cheat-ready boot, on a FAKE machine.

What the mode does (os/gentoo.sh, anticheatSetup): shim (Microsoft-signed) -> Gentoo's PREBUILT, Gentoo-signed unified
kernel image, lockdown=integrity via the UKI's @2 profile, root found by partition type, kexec off, Gentoo's key queued
for MOK enrolment. Approved by the user 2026-10-22. The live run on the ISO build VM that day found three bugs no fixture
had shown, and each is pinned here:
  * the PARENT DISK of the LUKS volume was looked up wrongly -- the root-type GUID must go on the partition under the
    unlocked root, on its own disk;
  * the kexec sysctl was "done" and never written (/etc/sysctl.d did not exist on a fresh install);
  * mokutil --import "succeeded" and queued nothing (the key is in the kernel's built-in keyring, so mokutil skips it
    and exits 0) -- it needs --ignore-keyring, and success is judged by `mokutil --list-new`, never the exit status.

gentoo.sh resets PATH at the top, so running the whole script would reach the REAL sfdisk/efibootmgr/emerge. These
tests therefore run the two SHIPPED functions, extracted verbatim, with stand-ins on PATH and PC_AC_ROOT as the root.
"""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")
KVER = "6.18.54"
ROOT_GUID = "4f68bce3-e8cd-4db1-96e7-fbcaf984b709"
pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="bash required")


def _fn(name):
    m = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name), GENTOO, re.S | re.M)
    assert m, f"{name} is not in os/gentoo.sh"
    return m.group(0)


def _exe(path, body):
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Machine:
    """A fake PosterChanOS install: UEFI, LUKS root on vda2 (unlocked as luks-x), ESP on vda1."""

    def __init__(self, tmp: Path, *, nvidia_gpu=False, nvidia_pkg=True, uefi=True, luks=True,
                 mok_queues=True, enrolled=False, image=True):
        self.r, self.bin, self.log = tmp / "root", tmp / "bin", tmp / "calls.log"
        self.bin.mkdir(parents=True); self.log.write_text("")
        r = self.r
        (r / "var/db/pkg/sys-kernel" / f"gentoo-kernel-bin-{KVER}").mkdir(parents=True)
        if nvidia_pkg:
            (r / "var/db/pkg/x11-drivers/nvidia-drivers-580").mkdir(parents=True)
        if uefi:
            (r / "sys/firmware/efi").mkdir(parents=True)
        gpu = r / "sys/bus/pci/devices/0000:01:00.0"; gpu.mkdir(parents=True)
        (gpu / "vendor").write_text("0x10de\n" if nvidia_gpu else "0x1af4\n"); (gpu / "class").write_text("0x030000\n")
        for blk, n in (("vda1", 1), ("vda2", 2)):
            (r / "sys/class/block" / blk).mkdir(parents=True); (r / "sys/class/block" / blk / "partition").write_text(f"{n}\n")
        (r / "etc").mkdir(parents=True, exist_ok=True)
        (r / "etc/crypttab").write_text("luks-x UUID=1234 /boot/keyfile.key luks\n")
        (r / "usr/src/linux/certs").mkdir(parents=True); (r / "usr/src/linux/certs/signing_key.x509").write_text("cert")
        (r / "usr/share/shim").mkdir(parents=True)
        for f in ("BOOTX64.EFI", "mmx64.efi"):
            (r / "usr/share/shim" / f).write_text(f)
        (r / "proc").mkdir(); (r / "proc/cpuinfo").write_text("vendor_id : AuthenticAMD\n")
        self.state = tmp / "mok-queued"
        L = f'echo "$(basename "$0") $*" >> "{self.log}"'
        _exe(self.bin / "id", '[ "$1" = -u ] && echo 1000 || /usr/bin/id "$@"')
        _exe(self.bin / "findmnt", f'{L}; case "$*" in *" /boot") echo /dev/vda1;; *) echo {"/dev/mapper/luks-x" if luks else "/dev/vda2"};; esac')
        _exe(self.bin / "cryptsetup", f'{L}; [ "$1" = status ] && [ "$2" = luks-x ] && echo "  device:  /dev/vda2"; true')
        # PKNAME of a PARTITION is its disk. Asked of the mapper device it would be the partition -- the 10-22 bug.
        _exe(self.bin / "lsblk", f'{L}; case "${{@: -1}}" in /dev/vda1|/dev/vda2) echo vda;; *) echo vda2;; esac')
        image_line = (f'mkdir -p "{r}/boot/EFI/posterchan"; echo uki > "{r}/boot/EFI/posterchan/posterchanos.efi"'
                      if image else "true")
        _exe(self.bin / "emerge", f'{L}; case "$*" in *gentoo-kernel-bin*) {image_line};; esac; true')
        for t in ("sfdisk",):
            _exe(self.bin / t, f'{L}; true')
        _exe(self.bin / "btrfs", f'{L}; [ "$2" = show ] && echo "        Subvolume ID:           256"; true')
        _exe(self.bin / "efibootmgr", f'{L}; [ $# -eq 0 ] && printf "BootCurrent: 0001\\nBoot0001* Linux Boot Manager\\nBoot0007* PosterChanOS (anti-cheat)\\n"; true')
        _exe(self.bin / "mokutil",
             f'{L}; case "$1" in --test-key) {"echo already enrolled" if enrolled else "echo is not enrolled"};; '
             f'--import) case "$*" in *--ignore-keyring*) {"touch " + str(self.state) if mok_queues else "true"};; '
             f'*) echo "Already in kernel trusted keyring. Skip";; esac;; '
             f'--list-new) [ -e "{self.state}" ] && echo "Subject: CN=Gentoo Linux kernel signing key";; esac; true')
        _exe(self.bin / "fake-gentoo-sh", f'{L}; true')

    def run(self, fn="anticheatSetup", **env):
        script = _fn("anticheatSetup") + _fn("anticheatOff") + f"\n{fn}\n"
        e = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", PC_AC_ROOT=str(self.r),
                 PC_GENTOO_SH=str(self.bin / "fake-gentoo-sh"), **env)
        p = subprocess.run(["bash", "-c", script], env=e, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr, self.log.read_text()


def test_a_ready_machine_is_converted(tmp_path):
    m = Machine(tmp_path)
    rc, out, calls = m.run()
    assert rc == 0, out
    r = m.r
    # Gentoo's signed UKI, kept at one path by the kernel-install plugin, behind shim.
    assert "sys-kernel/gentoo-kernel-bin generic-uki" in (r / "etc/portage/package.use/zz-posterchan-anticheat").read_text()
    plugin = r / "etc/kernel/install.d/95-posterchan-anticheat.install"
    assert plugin.exists() and os.access(plugin, os.X_OK)
    assert f"emerge -1 sys-kernel/installkernel =sys-kernel/gentoo-kernel-bin-{KVER} sys-boot/shim" in calls
    assert (r / "boot/EFI/posterchan/shimx64.efi").read_text() == "BOOTX64.EFI"
    assert (r / "boot/EFI/posterchan/mmx64.efi").exists()
    # BUG 1 (10-22): the root GUID goes on partition 2 of the DISK holding the LUKS volume.
    assert f"sfdisk --no-reread --part-type /dev/vda 2 {ROOT_GUID}" in calls, calls
    assert "btrfs subvolume set-default 256 /" in calls
    assert "# luks-x UUID=1234" in (r / "etc/crypttab").read_text()
    # The old entry of the same name is replaced, and the new one boots shim -> the UKI's lockdown profile @2.
    assert "efibootmgr -q -b 0007 -B" in calls
    assert "-l \\EFI\\posterchan\\shimx64.efi -u \\EFI\\posterchan\\posterchanos.efi @2" in calls
    # BUG 2 (10-22): kexec off is actually WRITTEN, on a machine with no /etc/sysctl.d.
    assert "kernel.kexec_load_disabled = 1" in (r / "etc/sysctl.d/99-posterchan-anticheat.conf").read_text()
    assert "fake-gentoo-sh sleep-policy 0" in calls
    assert (r / "etc/posterchan/anticheat").exists()
    # BUG 3 (10-22): the key is queued with --ignore-keyring, and the QUEUE is checked.
    assert f"mokutil --import {r}/usr/src/linux/certs/signing_key.x509 --ignore-keyring" in calls
    assert "mokutil --list-new" in calls
    assert "Enroll MOK" in out


def test_a_mokutil_that_queues_nothing_is_a_failure_not_a_done(tmp_path):
    m = Machine(tmp_path, mok_queues=False)
    rc, out, _ = m.run()
    assert rc == 1, out
    assert "nothing is queued" in out
    assert "Done." not in out


def test_an_already_trusted_key_is_not_queued_again(tmp_path):
    rc, out, calls = Machine(tmp_path, enrolled=True).run()
    assert rc == 0, out
    assert "--import" not in calls and "already trusted" in out


def test_an_nvidia_gpu_is_refused_but_the_package_alone_is_not(tmp_path):
    """Every install carries nvidia-drivers (one image boots any GPU); only a real NVIDIA display device matters."""
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir(); (tmp_path / "c").mkdir()
    rc, out, calls = Machine(tmp_path / "a", nvidia_gpu=True).run()
    assert rc == 1 and "NVIDIA GPU" in out and "emerge" not in calls
    rc, out, _ = Machine(tmp_path / "b", nvidia_gpu=True).run(PC_ANTICHEAT_FORCE="1")
    assert rc == 0, out
    rc, out, _ = Machine(tmp_path / "c", nvidia_gpu=False, nvidia_pkg=True).run()
    assert rc == 0, out


@pytest.mark.parametrize("what", ["uefi", "luks", "image"])
def test_a_machine_that_cannot_take_it_is_refused_before_it_is_changed(tmp_path, what):
    m = Machine(tmp_path, **{what: False})
    rc, out, calls = m.run()
    assert rc == 1, out
    assert "sfdisk" not in calls and "efibootmgr -q -c" not in calls, "the boot chain was changed on a refused machine"
    assert not (m.r / "etc/posterchan/anticheat").exists()


def test_anticheat_off_undoes_it(tmp_path):
    m = Machine(tmp_path)
    assert m.run()[0] == 0
    rc, out, calls = m.run("anticheatOff")
    assert rc == 0, out
    r = m.r
    for f in ("etc/portage/package.use/zz-posterchan-anticheat", "etc/kernel/install.d/95-posterchan-anticheat.install",
              "etc/sysctl.d/99-posterchan-anticheat.conf", "etc/posterchan/anticheat"):
        assert not (r / f).exists(), f
    assert (r / "etc/crypttab").read_text() == "luks-x UUID=1234 /boot/keyfile.key luks\n"
    assert calls.count("efibootmgr -q -b 0007 -B") == 2
    assert f"emerge -1 sys-kernel/installkernel =sys-kernel/gentoo-kernel-bin-{KVER}" in calls


def _plugin(tmp_path):
    """The kernel-install plugin anticheatSetup writes, with its absolute paths pointed into tmp."""
    m = Machine(tmp_path / "m")
    assert m.run()[0] == 0
    body = (m.r / "etc/kernel/install.d/95-posterchan-anticheat.install").read_text()
    boot, mods = tmp_path / "boot", tmp_path / "modules"
    body = body.replace("/boot/EFI/posterchan", str(boot / "EFI/posterchan")).replace("/usr/lib/modules", str(mods))
    p = tmp_path / "plugin"; _exe(p, body.split("\n", 1)[1])
    return p, boot / "EFI/posterchan", mods


def _uki(mods, ver):
    (mods / ver).mkdir(parents=True, exist_ok=True); (mods / ver / "uki.efi").write_text("uki " + ver)


def test_the_plugin_keeps_the_newest_signed_image_and_the_one_before(tmp_path):
    plugin, d, mods = _plugin(tmp_path)
    for v in ("6.18.48", "6.18.54"):
        _uki(mods, v)
    subprocess.run([str(plugin), "add", "6.18.48"], check=True)
    subprocess.run([str(plugin), "add", "6.18.54"], check=True)
    assert (d / "posterchanos.efi").read_text() == "uki 6.18.54" and (d / "posterchanos.version").read_text().strip() == "6.18.54"
    assert (d / "posterchanos-prev.efi").read_text() == "uki 6.18.48"
    # An OLDER kernel being (re)installed never replaces the newest.
    subprocess.run([str(plugin), "add", "6.18.48"], check=True)
    assert (d / "posterchanos.efi").read_text() == "uki 6.18.54"
    subprocess.run([str(plugin), "remove", "6.18.54"], check=True)
    assert (d / "posterchanos.efi").read_text() == "uki 6.18.54"


def test_the_mode_is_reachable_from_the_command_line():
    assert re.search(r'elif \[ "\$1" = "anticheat" \]; then\n\tanticheatSetup', GENTOO)
    assert re.search(r'elif \[ "\$1" = "anticheat-off" \]; then\n\tanticheatOff', GENTOO)
