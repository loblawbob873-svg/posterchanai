"""Already-installed PosterChanOS machines get pc-usb-grant through update-posterchan, i.e. from the
posterchanos-shell ebuild — not only from a fresh gentoo.sh install.

The ebuild's src_install is RUN under bash with the portage install helpers stubbed to record what lands where
(FILESDIR is the overlay's files/ plus what publish_overlay.sh injects), so a helper missing from the loop, a
sudoers file that is never installed, or a scanner left out of FILESDIR each fails here. The helper in files/
must be byte-identical to os/bin (the pc-kernel-guard pattern), the scanner is injected from the VM host's own
usb.py by publish_overlay.sh (one source, like bip340/bech32), and the sudoers rule the package ships is the one
gentoo.sh writes on a fresh install.
"""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "os/overlay/app-misc/posterchanos-shell"
EBUILD = next(PKG.glob("posterchanos-shell-*.ebuild"))
RULE = ("%posterchan ALL=(root) NOPASSWD: /usr/local/bin/pc-usb-grant grant *, "
        "/usr/local/bin/pc-usb-grant revoke *")


def _run_src_install(tmp_path):
    files = tmp_path / "files"
    shutil.copytree(PKG / "files", files)
    # what publish_overlay.sh injects before the package is built
    for name, src in (("bip340.py", "app/services/nostr/bip340.py"), ("bech32.py", "app/services/nostr/bech32.py"),
                      ("pc_usb_scan.py", "app/services/vmhost/usb.py"), ("gentoo.sh", "os/gentoo.sh"),
                      ("publish_iso.sh", "scripts/publish_iso.sh")):
        shutil.copy(ROOT / src, files / name)
    (files / "plymouth").mkdir(exist_ok=True)
    (files / "plymouth" / "x.png").write_text("")
    log = tmp_path / "installed"
    stubs = r'''
set -e
inherit(){ :; }; use(){ return 1; }; die(){ echo "die: $*" >&2; exit 1; }; einfo(){ :; }; ewarn(){ :; }
_d=/; insinto(){ _d="$1"; }; exeinto(){ _x="$1"; }
_rec(){ [ -e "$1" ] || { echo "missing source: $1" >&2; exit 3; }; echo "$2" >> "$LOG"; }
doins(){ for f in "$@"; do _rec "$f" "$_d/$(basename "$f")"; done; }
newins(){ _rec "$1" "$_d/$2"; }
doexe(){ for f in "$@"; do _rec "$f" "$_x/$(basename "$f")"; done; }
dobin(){ for f in "$@"; do _rec "$f" "/usr/bin/$(basename "$f")"; done; }
dosym(){ echo "sym $2" >> "$LOG"; }
fperms(){ echo "perms $1 $2" >> "$LOG"; }
'''
    script = stubs + f'source "{EBUILD}"\nsrc_install\n'
    env = dict(os.environ, FILESDIR=str(files), T=str(tmp_path), LOG=str(log), WORKDIR=str(tmp_path))
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return log.read_text().splitlines()


def test_the_shell_package_installs_the_helper_its_sudoers_and_the_scanner(tmp_path):
    got = _run_src_install(tmp_path)
    assert "/usr/local/bin/pc-usb-grant" in got
    assert "/usr/local/lib/posterchan/pc_usb_scan.py" in got
    assert "/etc/sudoers.d/posterchan-usb-grant" in got
    assert "perms 0440 /etc/sudoers.d/posterchan-usb-grant" in got


def test_the_packaged_helper_is_the_repositorys():
    assert (PKG / "files/pc-usb-grant").read_bytes() == (ROOT / "os/bin/pc-usb-grant").read_bytes()
    assert os.access(PKG / "files/pc-usb-grant", os.X_OK)


def test_the_packaged_rule_is_the_one_a_fresh_install_writes():
    lines = [ln for ln in (PKG / "files/posterchan-usb-grant.sudoers").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert lines == [RULE]
    assert f'"{RULE}"' in (ROOT / "os/gentoo.sh").read_text()


def test_the_scanner_is_injected_from_the_vm_hosts_own_module():
    pub = (ROOT / "scripts/publish_overlay.sh").read_text()
    assert ('"$(dirname "$SRC")/../app/services/vmhost/usb.py" \\\n'
            '  "$TMP/app-misc/posterchanos-shell/files/pc_usb_scan.py"') in pub
    assert not (PKG / "files/pc_usb_scan.py").exists(), "a hand-kept second copy of the scanner would drift"
