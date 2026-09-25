"""libvirt's secret key is never EMPTY on a PosterChanOS machine -- or the VMs app is dead on every install.

Found 2026-09-25 by the release gate's server stage: every freshly installed machine booted `degraded`
with libvirtd failing "Failed at step CREDENTIALS ... Bad message". The chain:
  1. the ISO carried the BUILD host's /var/lib/systemd/credential.secret (bound to its machine id);
  2. on first boot libvirt's virt-secret-init-encryption ran `systemd-creds encrypt`, which found it
     ("comes from a different machine ID, deleting") and the pipe wrote a 0-byte key;
  3. the stock unit's ConditionPathExists=! then never ran again, so the empty key was permanent.
Fixed three ways, each tested here: the image excludes the secret, the installer removes a stale one
(and an empty key), and a vendor drop-in re-makes an empty key -- atomically, so it cannot happen again.
Verified on the gate's real broken disk: key 0 bytes -> libvirtd active, virsh answering.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
G = (ROOT / "os/gentoo.sh").read_text()
DROPIN = ROOT / "os/overlay/app-misc/posterchanos-shell/files/virt-secret-init-encryption-posterchanos.conf"
KEY = "/var/lib/libvirt/secrets/secrets-encryption-key"


def test_the_image_does_not_carry_the_build_hosts_credential_secret():
    excludes = G[G.index("local EXCLUDES=("):]
    excludes = excludes[:excludes.index("\n\t)")]
    lines = [l.strip() for l in excludes.splitlines() if l.strip() and not l.strip().startswith("#")]
    assert any("var/lib/systemd/credential.secret" in l.split() for l in lines)


def _cleanup_block():
    body = G[G.index("liveISOinstall() {"):]
    return body[body.index("# THE LIVE SESSION'S OWN ACCOUNT DOES NOT BELONG ON AN INSTALL."):
                body.index("# ROOT MUST BE ABLE TO LOG IN BEFORE ANYTHING ELSE IS TRIED.")]


def test_the_installer_removes_a_stale_secret_and_an_empty_key_but_keeps_a_real_key(tmp_path):
    for case, key in (("empty", b""), ("real", b"sealed-key-bytes")):
        t = tmp_path / case
        (t / "etc").mkdir(parents=True)
        for f in ("passwd", "shadow", "group"):
            (t / "etc" / f).write_text("root:x:0:0::/root:/bin/bash\n")
        (t / "var/lib/systemd").mkdir(parents=True)
        (t / "var/lib/systemd/credential.secret").write_bytes(b"build-host-secret")
        (t / "var/lib/libvirt/secrets").mkdir(parents=True)
        (t / KEY.lstrip("/")).write_bytes(key)
        r = subprocess.run(["bash", "-c", "sudo() { \"$@\"; }\nTARGET=%s\n%s" % (t, _cleanup_block())],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert not (t / "var/lib/systemd/credential.secret").exists(), "the build host's secret was installed"
        assert (t / KEY.lstrip("/")).exists() == (case == "real"), case


def _execstart():
    """The drop-in's command as systemd would run it: `$$` -> `$`, `%%` -> `%` (and no other specifier)."""
    lines = [l for l in DROPIN.read_text().splitlines() if l.startswith("ExecStart=/")]
    assert len(lines) == 1
    cmd = lines[0][len("ExecStart="):]
    assert not re.search(r"(?<!\$)\$(?!\$)[A-Za-z{(]", cmd.replace("$$", "")), "an unescaped $ systemd would expand"
    assert not re.search(r"%(?!%)", cmd.replace("%%", "")), "an unescaped % specifier"
    return cmd.replace("$$", "$").replace("%%", "%")


def _run(tmp_path, key_bytes, creds_ok=True):
    root = tmp_path / ("ok" if creds_ok else "fail")
    key = root / KEY.lstrip("/")
    key.parent.mkdir(parents=True)
    if key_bytes is not None:
        key.write_bytes(key_bytes)
    bindir = root / "bin"
    bindir.mkdir()
    creds = bindir / "systemd-creds"
    creds.write_text("#!/bin/sh\ncat >/dev/null\n" + ("printf SEALED >\"$4\"\n" if creds_ok else ": >\"$4\"; exit 1\n"))   # the real failure: an EMPTY file, then an error
    creds.chmod(creds.stat().st_mode | stat.S_IEXEC)
    cmd = _execstart()
    assert cmd.startswith("/usr/bin/sh -c '") and cmd.endswith("'")
    # The directory first and by its whole `mkdir` argument: the key path CONTAINS the directory path.
    script = cmd[len("/usr/bin/sh -c '"):-1].replace("mkdir -p /var/lib/libvirt/secrets;", f"mkdir -p {key.parent};")
    script = script.replace(KEY, str(key))
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"))
    return r.returncode, key


def test_an_empty_key_is_made_again_and_a_good_one_is_left_alone(tmp_path):
    rc, key = _run(tmp_path, b"")
    assert rc == 0 and key.read_bytes() == b"SEALED", "an empty key was not regenerated"
    rc, key = _run(tmp_path / "g", b"KEEP")
    assert rc == 0 and key.read_bytes() == b"KEEP", "a real key was replaced -- every stored secret would be lost"


def test_a_failed_encrypt_never_leaves_an_empty_key_behind(tmp_path):
    rc, key = _run(tmp_path, None, creds_ok=False)
    assert rc != 0, "a failed encrypt reported success"
    assert not key.exists(), "a failed encrypt left a key file -- the empty-key trap again"
    assert not Path(str(key) + ".new").exists()


def test_the_drop_in_replaces_the_stock_condition_and_is_installed():
    text = DROPIN.read_text()
    assert re.search(r"^\[Unit\]\s*\nConditionPathExists=\s*$", text, re.M), "the stock ConditionPathExists=! still applies"
    assert re.search(r"^ExecStart=\s*$", text, re.M), "the stock ExecStart is not reset"
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0-r5.ebuild").read_text()
    assert "insinto /usr/lib/systemd/system/virt-secret-init-encryption.service.d" in ebuild
    assert 'newins "${FILESDIR}/virt-secret-init-encryption-posterchanos.conf" 50-posterchanos.conf' in ebuild
