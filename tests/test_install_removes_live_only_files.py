"""An INSTALLED machine carries nothing that only makes sense on the live disc.

The installer copies the live image's root onto the disk, so every live-only file the image adds comes
along unless the installer removes it. The tty1 autologin of the `live` account was removed; the SERIAL
console's, added to the image later, was not -- so every installed machine's ttyS0 tried to autologin an
account that no longer exists, about once a second, for ever ("User not known to the underlying
authentication module"). Found 2026-09-24 by the release gate's server stage, the first thing ever to log
in over that console after an install. The live network gate (multi-user.target REQUIRING NetworkManager)
and the live motd ("Install to this machine: …") leaked the same way.

The list of live-only files is DERIVED from the image build, so one added later is covered the day it is.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
G = (ROOT / "os/gentoo.sh").read_text()

# What the image adds that belongs to the live session: its account's files, its autologins, its boot
# gate, its motd. (etc/hostname and the launcher are not live-only: an installed machine needs both.)
LIVE_ONLY = re.compile(r"(^|/)(getty@tty1\.service\.d|serial-getty@[^/]+\.service\.d)/|live|^etc/motd$")


def _live_only_pseudo_files():
    paths = re.findall(r'pseudoput "([^"]+)" f ', G)
    return sorted({p for p in paths if LIVE_ONLY.search(p)})


def _cleanup_block():
    body = G[G.index("liveISOinstall() {"):]
    return body[body.index("# THE LIVE SESSION'S OWN ACCOUNT DOES NOT BELONG ON AN INSTALL."):
                body.index("# ROOT MUST BE ABLE TO LOG IN BEFORE ANYTHING ELSE IS TRIED.")]


def test_every_live_only_file_the_image_adds_is_removed_by_the_installer():
    live = _live_only_pseudo_files()
    assert "etc/systemd/system/serial-getty@ttyS0.service.d/override.conf" in live   # the derivation works
    block = _cleanup_block().replace("\\\n", " ")
    missing = [p for p in live if f"$TARGET/{p}" not in block]
    assert not missing, f"the installer leaves these live-disc files on every installed machine: {missing}"


def test_the_cleanup_actually_removes_them_and_keeps_a_motd_somebody_wrote(tmp_path):
    """RUN the installer's cleanup against a disk holding every live-only file (stub sudo)."""
    live = _live_only_pseudo_files()
    for case, motd in (("live", "\n  PosterChanOS live session.\n  Install to this machine: …\n"),
                       ("own", "Welcome to the family server.\n")):
        target = tmp_path / case
        for p in live:
            f = target / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x\n")
        (target / "etc/motd").write_text(motd)
        for f in ("etc/passwd", "etc/shadow", "etc/group"):
            (target / f).write_text("root:x:0:0::/root:/bin/bash\nlive:x:1000:1000::/home/live:/bin/bash\n")
        script = "sudo() { \"$@\"; }\nTARGET=%s\n%s" % (target, _cleanup_block())
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        left = [p for p in live if p != "etc/motd" and (target / p).exists()]
        assert not left, f"still on the installed disk: {left}"
        assert "live:" not in (target / "etc/passwd").read_text()
        if case == "live":
            assert not (target / "etc/motd").exists(), "the live motd is shown at every login of the install"
        else:
            assert (target / "etc/motd").read_text() == motd, "a motd somebody wrote was deleted"


def test_group_members_with_no_account_on_the_disk_are_dropped(tmp_path):
    """The build host's users stayed listed as group MEMBERS on every install -- `wheel` included -- and a
    `pc-<hash>` name comes from a Nostr key, so that key became an admin on any machine installed from the
    image. RUN the cleanup on exactly the lines found on a fresh install (2026-09-25)."""
    target = tmp_path / "t"
    (target / "etc").mkdir(parents=True)
    (target / "etc/passwd").write_text("root:x:0:0::/root:/bin/bash\nposterchan:x:999:999::/home/posterchan:/bin/bash\n"
                                        "pc-bcd1b0b7366431f5:x:1001:1001::/home/pc-bcd1b0b7366431f5:/bin/bash\n")
    (target / "etc/shadow").write_text("root:!:1::::::\n")
    (target / "etc/group").write_text("root:x:0:root\nwheel:x:10:root,verita84,live,pc-5ac337fb7cb82127,live,pc-bcd1b0b7366431f5\n"
                                       "video:x:27:live,pc-5ac337fb7cb82127,verita84,posterchan\nkvm:x:78:pc-5ac337fb7cb82127\nempty:x:5:\n")
    (target / "etc/gshadow").write_text("wheel:!:verita84,root:root,verita84,pc-bcd1b0b7366431f5\nvideo:!::live,posterchan\n")
    (target / "etc/group").chmod(0o644)
    (target / "etc/gshadow").chmod(0o600)
    script = "sudo() { \"$@\"; }\nTARGET=%s\n%s" % (target, _cleanup_block())
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    group = (target / "etc/group").read_text().splitlines()
    assert "wheel:x:10:root,pc-bcd1b0b7366431f5" in group, group
    assert "video:x:27:posterchan" in group and "kvm:x:78:" in group and "empty:x:5:" in group, group
    gsh = (target / "etc/gshadow").read_text().splitlines()
    assert gsh == ["wheel:!:root:root,pc-bcd1b0b7366431f5", "video:!::posterchan"], gsh
    assert oct((target / "etc/gshadow").stat().st_mode & 0o777) == "0o600", "gshadow must stay private"
    assert not list((target / "etc").glob("*.pcnew"))
