"""THE INSTALLED SYSTEM HAS A /tmp BEFORE ANYTHING RUNS INSIDE IT.

2026-10-31: every install gate failed (install, USB, GUI, server, upgrade). The live image carries no /tmp (a tmpfs at
runtime; the copy excludes it), and the only `mkdir $TARGET/tmp` sat at the very end, at the desktop download. So
pc-boot-check -- in the install path since 10-29 -- ran `mktemp` in the target and got "No such file or directory",
reported "the key inside the initramfs does not open /dev/vda2", and put the previous boot entry back; the overlay sync
could not write its log either. /tmp (1777) is now made right after the copy, before the first chroot.
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "os" / "gentoo.sh").read_text()


def _body(name):
    i = SRC.index(name + "() {")
    return SRC[i:SRC.index("\n}\n", i)]


def test_tmp_is_made_with_sticky_permissions_before_the_first_chroot():
    body = _body("liveISOinstall")
    mk = body.index('sudo mkdir -p "$TARGET/tmp"')
    assert body.index('sudo chmod 1777 "$TARGET/tmp"') > mk
    first_chroot = body.index("chroot $TARGET")
    assert mk < first_chroot, "the target's /tmp must exist before anything runs inside the target"
    copy = body.index("--exclude=/boot/***")
    assert copy < mk, "made after the copy (which excludes /tmp's contents)"
