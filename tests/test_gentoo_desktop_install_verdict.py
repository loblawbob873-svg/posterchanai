"""The installer's desktop step says what HAPPENED, and an unpacked desktop gets its launcher.

Found 2026-10-27 by the from-scratch VM build (check_scratch_install_vm.py): "could not download the PosterChan
desktop -- Wayfire will start with no shell", on a machine where the desktop had downloaded AND unpacked. The unpack
deletes the archive it unpacked, and the verdict then asked whether the archive still existed. That used to work
because the step ran from the HOST with TARGET=/tmp/install and the unpack deleted the target's COPY; finalize now
runs it inside the chroot with TARGET=/, where the archive is the only file. So /opt/posterchan existed, and the
launcher every session execs (/usr/local/bin/posterchan) was never written.

The block is cut out of the shipped os/gentoo.sh and RUN, in the chroot shape (the archive and its copy are one
file), against a sandbox: absolute paths are moved under tmp_path, and curl is a stub answering exactly what
poster.place and GitHub answered in that VM (poster.place: an HTML page; GitHub: the release listing, then a real
zstd tarball).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
G = (ROOT / "os/gentoo.sh").read_text()

pytestmark = pytest.mark.skipif(not shutil.which("zstd"), reason="zstd required to build the fixture tarball")


def _block():
    start = G.index('echo -e "\\033[1;33mInstalling the PosterChan desktop\\033[0m"')
    end = G.index("# ANYONE MAY SIGN IN, so an account has to exist", start)
    return G[start:end]


def _sandboxed(block, sb):
    # ONE pass: tmp_path itself lives under /tmp/, so replacing the prefixes one after another would rewrite the
    # sandbox paths a second time.
    return re.sub(r"/usr/local/bin|/usr/share|/opt|/tmp/", lambda m: f"{sb}{m.group(0)}", block)


CURL = r"""#!/bin/bash
out=""; url=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; -*) shift;; *) url="$1"; shift;; esac; done
case "$url" in
  *poster.place/desktop/*) body='<!doctype html><title>PosterChan for desktop</title>';;
  *api.github.com*) echo '{"browser_download_url":"https://github.com/x/releases/download/desktop-latest/PosterChan-1.0.9-linux-x64.tar.zst"}'; exit 0;;
  *github.com*linux-x64.tar.zst) [ "$PC_FAKE_GITHUB" = up ] || { echo "curl: (22) 404" >&2; exit 22; }; cp "$PC_FAKE_TAR" "$out"; exit 0;;
  *) echo "curl: (22) 404" >&2; exit 22;;
esac
[ -n "$out" ] && printf '%s' "$body" > "$out" || printf '%s' "$body"
"""


def _run(tmp_path, github):
    sb = tmp_path / "root"
    for d in ("tmp", "opt", "usr/local/bin", "usr/share"):
        (sb / d).mkdir(parents=True, exist_ok=True)
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "curl").write_text(CURL)
    (stub / "chown").write_text("#!/bin/sh\nexit 0\n")      # chown root:root needs root; nothing else does
    for f in ("curl", "chown"):
        (stub / f).chmod(0o755)
    # A real desktop tree, packed exactly as CI packs it (tar -caf ...tar.zst).
    tree = tmp_path / "linux-unpacked"
    (tree / "resources/tor/tor").mkdir(parents=True)
    for f in ("posterchan-desktop", "chrome-sandbox", "resources/tor/tor/tor"):
        (tree / f).write_text("#!/bin/sh\n")
    tar = tmp_path / "PosterChan-1.0.9-linux-x64.tar.zst"
    subprocess.run(["tar", "-C", str(tree), "-caf", str(tar), "."], check=True)
    # The chroot shape. Finalize runs this with TARGET=/; the block reads an empty TARGET the same way
    # (`${TARGET:-/}`), and empty leaves every absolute path to the sandbox rewrite. There is no copy into
    # the target, so the unpack's rm takes the only archive.
    script = "TARGET=\n_in() { /bin/bash -c \"$1\"; }\n%s" % _sandboxed(_block(), sb)
    env = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}", PC_FAKE_TAR=str(tar), PC_FAKE_GITHUB=github)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)
    return sb, r.stdout + r.stderr


def test_a_desktop_that_downloaded_and_unpacked_gets_its_launcher(tmp_path):
    sb, out = _run(tmp_path, "up")
    assert (sb / "opt/posterchan/posterchan-desktop").exists(), ("the desktop was not unpacked", out[-1500:])
    assert "could not download" not in out, ("a desktop that downloaded and unpacked is reported as never downloaded",
                                             out[-1500:])
    launcher = sb / "usr/local/bin/posterchan"
    assert launcher.exists() and os.access(launcher, os.X_OK), \
        ("no /usr/local/bin/posterchan: every session execs it, so the machine boots to no shell", out[-1500:])
    assert "/opt/posterchan" in launcher.read_text()
    assert re.search(r"✓ \S*/usr/local/bin/posterchan", out), ("the install did not say the launcher is there", out[-800:])


def test_nothing_downloadable_still_says_so_with_its_reason(tmp_path):
    sb, out = _run(tmp_path, "down")
    assert "could not download the PosterChan desktop" in out, out[-1500:]
    assert "poster.place tarball is not the file it claims" in out, "the reason was not shown"
    assert not (sb / "usr/local/bin/posterchan").exists(), "a launcher was written for a desktop that is not there"
