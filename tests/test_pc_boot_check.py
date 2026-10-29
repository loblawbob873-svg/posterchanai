"""pc-boot-check: will this machine's boot entries start this machine -- asked BEFORE the restart.

"this is a common problem with you screwing up the bootloader and encryption ... we need to prevent this"
(2026-10-29). The laptop that day: `gentoo.sh bootloader` wrote `root=UUID=` with no value into the entry and the
initramfs and reported success; the machine waited on /dev/disk/by-uuid for ever. The checker is RUN here against
a fake /boot with stub tools (lsinitrd reads a listing file and a directory of files; cryptsetup opens the disk
only with a key that reads GOOD), one case per way an entry has been wrong, and the healthy machine must pass.
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "os/bin/pc-boot-check"
MID = "6ed1a9cb0331488fb15f91fb73891620"
KV = "6.18.57-gentoo-dist-bin"
FS = "a02705bc-fe2d-42c6-a425-a16acfbd5eef"
LUKS = "44dc2563-2b35-4dc1-ba09-a95bb9aa158a"
GOOD = f"quiet splash rd.luks.key=/boot/keyfile.key root=UUID={FS} rootflags=subvol=@gentoo rw rd.luks.uuid=luks-{LUKS}"


def _exe(p, body):
    p.write_text("#!/bin/bash\n" + body + "\n"); p.chmod(0o755)


def machine(tmp, *, options=GOOD, key_in_image="GOOD", image_root=f"UUID={FS}", kernel=True, entry=True):
    r, b = tmp / "root", tmp / "bin"
    (r / "etc").mkdir(parents=True, exist_ok=True); b.mkdir()
    (r / "etc/machine-id").write_text(MID + "\n")
    kd = r / "boot" / MID / KV
    kd.mkdir(parents=True, exist_ok=True)
    if kernel:
        (kd / "linux").write_text("bzImage")
    files = {"usr/bin/systemd-cryptsetup": "x"}
    if key_in_image is not None:
        files["boot/keyfile.key"] = key_in_image
    if image_root is not None:
        files["etc/cmdline.d/10-default.conf"] = f" quiet root={image_root} rw "
    (kd / "initrd").write_text("".join(f"-rw-r--r-- 1 root root 1 Oct 29 11:18 {f}\n" for f in files))
    for f, text in files.items():
        p = tmp / "img" / f; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
    (r / "boot/loader/entries").mkdir(parents=True, exist_ok=True)
    if entry:
        (r / "boot/loader/entries" / f"{MID}-{KV}.conf").write_text(
            f"title Current\nversion {KV}\noptions {options}\nmachine-id {MID}\n"
            f"linux /{MID}/{KV}/linux\ninitrd /{MID}/{KV}/initrd\n")
    _exe(b / "lsinitrd", f'if [ "$1" = -f ]; then cat "{tmp}/img/$2"; else cat "$1"; fi')
    _exe(b / "blkid", f'case "$*" in "-t UUID={LUKS} -o device") echo /dev/nvme0n1p2;; '
                      f'"-s TYPE -o value /dev/nvme0n1p2") echo crypto_LUKS;; esac')
    _exe(b / "cryptsetup", 'for a in "$@"; do k="$a"; prev="$p"; p="$a"; [ "$prev" = --key-file ] && kf="$a"; done; '
                           '[ "$(cat "$kf" 2>/dev/null)" = GOOD ]')
    env = dict(os.environ, PATH=f"{b}:/usr/bin:/bin", PC_ROOT=str(r), PC_ROOT_UUID=FS)
    p = subprocess.run(["bash", str(CHECK)], capture_output=True, text=True, timeout=30, env=env)
    return p.returncode, p.stdout + p.stderr


def test_the_healthy_machine_passes(tmp_path):
    rc, out = machine(tmp_path)
    assert rc == 0, out
    assert "starts this machine and unlocks its disk by itself" in out


def test_the_laptops_empty_root_is_caught(tmp_path):
    rc, out = machine(tmp_path, options=GOOD.replace(f"root=UUID={FS}", "root=UUID="), image_root="UUID=")
    assert rc == 1 and "root=UUID= has no value" in out and "initramfs itself carries root=UUID=" in out, out


def test_a_root_that_is_not_this_machines_is_caught(tmp_path):
    rc, out = machine(tmp_path, options=GOOD.replace(FS, "1111-2222"), image_root="UUID=1111-2222")
    assert rc == 1 and "the file system mounted at / is UUID=" + FS in out, out


def test_an_image_without_the_key_is_caught(tmp_path):
    rc, out = machine(tmp_path, key_in_image=None)
    assert rc == 1 and "not inside the initramfs" in out, out


def test_a_key_in_the_image_that_does_not_open_the_disk_is_caught(tmp_path):
    rc, out = machine(tmp_path, key_in_image="an old rotated key")
    assert rc == 1 and "does not open /dev/nvme0n1p2" in out, out


def test_an_encrypted_partition_that_does_not_exist_is_caught(tmp_path):
    rc, out = machine(tmp_path, options=GOOD.replace(LUKS, "dead-beef"))
    assert rc == 1 and "names no partition" in out, out


def test_a_missing_kernel_is_caught(tmp_path):
    rc, out = machine(tmp_path, kernel=False)
    assert rc == 1 and "its kernel" in out, out


def test_a_machine_with_no_entry_of_its_own_is_caught(tmp_path):
    rc, out = machine(tmp_path, entry=False)
    assert rc == 1 and "no boot entry for this machine" in out, out


def test_a_password_entry_is_not_a_problem(tmp_path):
    """Travel mode / a password install: no rd.luks.key, so the start asks -- by design."""
    rc, out = machine(tmp_path, options=GOOD.replace("rd.luks.key=/boot/keyfile.key ", ""), key_in_image=None)
    assert rc == 0, out


def test_it_is_installed_beside_pc_kernel_guard():
    from tests.overlay_paths import shell_ebuild
    files = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-boot-check"
    assert files.read_bytes() == CHECK.read_bytes(), "the overlay copy drifted from os/bin"
    assert "pc-boot-check" in shell_ebuild().read_text().split("for helper in", 1)[1].split(";", 1)[0]


def test_in_travel_mode_a_key_less_image_is_right(tmp_path):
    (tmp_path / "root/etc/posterchan/travel").mkdir(parents=True)
    (tmp_path / "root/etc/posterchan/travel/on").write_text("")
    rc, out = machine(tmp_path, key_in_image=None)
    assert rc == 0 and "asks for the disk password" in out, out


def test_in_travel_mode_a_key_left_in_any_image_is_caught(tmp_path):
    """Including an old kernel's image with no entry -- the laptop's 6.18.43/.48."""
    (tmp_path / "root/etc/posterchan/travel").mkdir(parents=True)
    (tmp_path / "root/etc/posterchan/travel/on").write_text("")
    old = tmp_path / "root/boot" / MID / "6.18.43-gentoo-dist-bin"
    old.mkdir(parents=True)
    (old / "initrd").write_text("-rwx------ 1 root root 4096 Sep 17 13:54 boot/keyfile.key\n")
    rc, out = machine(tmp_path, key_in_image=None)
    assert rc == 1 and "6.18.43-gentoo-dist-bin/initrd still holds the key file" in out, out


def test_in_travel_mode_the_key_on_the_boot_partition_is_caught(tmp_path):
    (tmp_path / "root/etc/posterchan/travel").mkdir(parents=True)
    (tmp_path / "root/etc/posterchan/travel/on").write_text("")
    (tmp_path / "root/boot").mkdir(parents=True)
    (tmp_path / "root/boot/keyfile.key").write_text("k")
    rc, out = machine(tmp_path, key_in_image=None)
    assert rc == 1 and "still on the boot partition" in out, out
