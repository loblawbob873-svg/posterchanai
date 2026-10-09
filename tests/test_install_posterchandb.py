"""scripts/install/posterchandb.sh RUN under bash with chattr/lsattr/stat stubbed: the data directory is created,
made NOCOW (`chattr -R +C`) on btrfs and only there, the C attribute is re-read rather than assumed, and the path
comes from POSTERCHANDB_DIR / data/secrets.env exactly as the app resolves it."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STUBS = {
    "chattr": 'echo "chattr $*" >> "$STUB_LOG"; [ "${STUB_CHATTR_FAIL:-0}" = 1 ] && exit 1; echo "${@: -1}" >> "$STUB_STATE/nocow"',
    "lsattr": ('d="${@: -1}"; if grep -qxF "$d" "$STUB_STATE/nocow" 2>/dev/null || grep -qxF "$(dirname "$d")" "$STUB_STATE/nocow" 2>/dev/null; '
               'then echo "---------------C------ $d"; else echo "---------------------- $d"; fi'),
    "stat": 'if [ "$1" = "-f" ]; then echo "$STUB_FSTYPE"; else exec /usr/bin/stat "$@"; fi',
    "sudo": 'exec "$@"',
}


def run(tmp_path, fstype="btrfs", env=None, secrets=None, chattr_fail=False):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        f = bin_ / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True, exist_ok=True)
    if secrets is not None:
        (repo / "data" / "secrets.env").write_text(secrets)
    log = tmp_path / "log"
    script = ('print_step(){ echo "STEP $*"; }; print_success(){ echo "OK $*"; }; print_warning(){ echo "WARN $*"; }; '
              '. "%s/scripts/install/posterchandb.sh"; setup_posterchandb_dir' % ROOT)
    e = dict(os.environ, PATH="%s:%s" % (bin_, os.environ["PATH"]), STUB_LOG=str(log), STUB_STATE=str(state),
             STUB_FSTYPE=fstype, SCRIPT_DIR=str(repo), STUB_CHATTR_FAIL="1" if chattr_fail else "0")
    e.pop("POSTERCHANDB_DIR", None)
    e.update(env or {})
    out = subprocess.run(["bash", "-c", script], env=e, capture_output=True, text=True, timeout=30)
    return out.stdout + out.stderr, (log.read_text() if log.exists() else ""), repo


def test_btrfs_gets_recursive_nocow_and_it_is_verified(tmp_path):
    out, log, repo = run(tmp_path)
    d = repo / "data" / "posterchandb"
    assert d.is_dir() and oct(d.stat().st_mode & 0o777) == "0o700"
    assert "chattr -R +C %s" % d in log
    assert "OK NOCOW set" in out


def test_not_btrfs_sets_nothing(tmp_path):
    out, log, _ = run(tmp_path, fstype="ext2/ext3")
    assert "chattr" not in log
    assert "btrfs-only" in out


def test_a_failed_chattr_is_reported_not_claimed(tmp_path):
    out, _, _ = run(tmp_path, chattr_fail=True)
    assert "WARN chattr -R +C failed" in out and "OK NOCOW" not in out


def test_the_directory_comes_from_secrets_env_like_the_app_reads_it(tmp_path):
    target = tmp_path / "raid" / "posterchandb"
    out, log, _ = run(tmp_path, secrets='export FOO=1\nexport POSTERCHANDB_DIR="%s"\n' % target)
    assert target.is_dir() and "chattr -R +C %s" % target in log
    env_target = tmp_path / "envdir"
    out, log, _ = run(tmp_path, env={"POSTERCHANDB_DIR": str(env_target)}, secrets="export POSTERCHANDB_DIR=/nope\n")
    assert env_target.is_dir(), "the environment wins over the file, as in the app"


def test_the_app_resolves_the_same_directory(monkeypatch):
    from app.services import posterchandb
    monkeypatch.setenv("POSTERCHANDB_DIR", "/usb/posterchandb")
    assert posterchandb.data_dir() == "/usb/posterchandb"
    monkeypatch.delenv("POSTERCHANDB_DIR")
    assert posterchandb.data_dir() == str(ROOT / "data" / "posterchandb")


def test_every_install_path_prepares_it_and_posterchanos_points_at_a_nocow_dir():
    inst = (ROOT / "install.sh").read_text()
    assert inst.count("setup_posterchandb_dir") >= 3 and 'source "$INSTALL_DIR/posterchandb.sh"' in inst
    g = (ROOT / "os" / "gentoo.sh").read_text()
    assert '"/var/lib/posterchandb"' in g.split("DISABLE_COW=(", 1)[1].split(")", 1)[0]
    pcs = (ROOT / "os" / "bin" / "pc-server").read_text()
    assert 'set_secret POSTERCHANDB_DIR "/var/lib/posterchandb"' in pcs
    assert "POSTERCHANDB_DIR=/var/lib/posterchanai/posterchandb" in (ROOT / "Dockerfile").read_text()
    assert 'chattr -R +C "$PCDB_DIR"' in (ROOT / "docker-entrypoint.sh").read_text()
