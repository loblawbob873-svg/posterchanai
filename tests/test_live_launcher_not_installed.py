"""The live USB's "Install PosterChanOS" launcher must not live on an installed machine.

"Install PosterChan not doing anything on laptop": livecd writes posterchanos-install.desktop into
the IMAGE, install-live copied the image onto the disk, so every installed machine carried a start
menu entry that opens a terminal running `sudo gentoo.sh`. The install now strips it, and the
updater removes the leftover from machines installed before — RUN here against a fake cmdline.
"""
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPD = ROOT / "os/bin/update-posterchan"


def _snippet():
    s = UPD.read_text()
    a = s.index("_pc_live_launcher=")
    return s[a:s.index("\nfi\n", a) + 4]


def _run(tmp, cmdline):
    f = tmp / "posterchanos-install.desktop"
    f.write_text("[Desktop Entry]\n")
    c = tmp / "cmdline"
    c.write_text(cmdline)
    script = "CYA=; OFF=\n" + _snippet()
    subprocess.run([shutil.which("bash"), "-c", script], check=True, timeout=20,
                   env={"PATH": "/usr/bin:/bin", "PC_LIVE_LAUNCHER": str(f), "PC_PROC_CMDLINE": str(c)})
    return f.exists()


def test_an_installed_machine_loses_the_leftover(tmp_path):
    assert not _run(tmp_path, "BOOT_IMAGE=/vmlinuz root=UUID=x rw quiet\n")


def test_a_live_boot_keeps_it(tmp_path):
    assert _run(tmp_path, "BOOT_IMAGE=/boot/vmlinuz root=live:CDLABEL=POSTERCHAN rd.live.image quiet\n")


def test_the_install_strips_it_and_both_updater_copies_agree():
    g = (ROOT / "os/gentoo.sh").read_text()
    assert "rm -f $TARGET/usr/share/applications/posterchanos-install.desktop" in g
    assert UPD.read_bytes() == (ROOT / "os/overlay/app-misc/posterchanos-shell/files/update-posterchan").read_bytes()
