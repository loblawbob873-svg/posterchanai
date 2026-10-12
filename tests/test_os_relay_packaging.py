"""How PosterChanOS installs and starts its local relay (app-misc/posterchan-relay).

* the launcher (os/bin/pc-local-relay, one source; the overlay's copy is the same bytes) makes the cache
  directory NOCOW on btrfs -- recursively while empty, the directory alone once it holds data -- and refuses
  with exit 78 (which the unit does not restart on) when the code or the Python modules are missing;
* the USER unit runs it, is ordered before the graphical session and enabled globally by the package;
* the session package depends on it, and publish_overlay.sh injects the launcher and re-versions the package.
The launcher is RUN under sh with stub `stat`/`chattr`/python on PATH.
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "os/bin/pc-local-relay"
PKG = ROOT / "os/overlay/app-misc/posterchan-relay"
UNIT = PKG / "files/posterchan-relay.service"


def _stubs(tmp, fs="btrfs", modules_ok=True):
    bin_ = tmp / "bin"
    bin_.mkdir()
    log = tmp / "calls.log"
    (bin_ / "stat").write_text(f'#!/bin/sh\necho "{fs}"\n')
    (bin_ / "chattr").write_text(f'#!/bin/sh\necho "chattr $*" >>"{log}"\n')
    (bin_ / "python3").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ]; then {"exit 0" if modules_ok else "exit 1"}; fi\n'
        f'echo "python $* in $(pwd) dir=$PC_RELAY_DIR" >>"{log}"\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    return bin_, log


def _run(tmp, bin_, code, data):
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp), "PC_RELAY_CODE": str(code),
           "PC_RELAY_DIR": str(data), "PC_RELAY_PYTHON": "python3"}
    return subprocess.run(["sh", str(LAUNCHER)], capture_output=True, text=True, timeout=30, env=env)


def _code(tmp):
    code = tmp / "code"
    code.mkdir()
    (code / "relay_main.py").write_text("")
    return code


def test_an_empty_cache_dir_on_btrfs_is_made_nocow_recursively_then_the_relay_starts(tmp_path):
    bin_, log = _stubs(tmp_path)
    data = tmp_path / "relay"
    out = _run(tmp_path, bin_, _code(tmp_path), data)
    assert out.returncode == 0, out.stderr
    calls = log.read_text().splitlines()
    assert calls[0] == f"chattr -R +C {data}", calls
    assert calls[1].startswith("python relay_main.py --os-mode in ") and f"dir={data}" in calls[1], calls
    assert oct(data.stat().st_mode & 0o777) == "0o700"


def test_a_cache_dir_holding_data_gets_nocow_on_the_directory_only(tmp_path):
    bin_, log = _stubs(tmp_path)
    data = tmp_path / "relay"
    data.mkdir()
    (data / "seg-000000.log").write_text("x")
    assert _run(tmp_path, bin_, _code(tmp_path), data).returncode == 0
    assert log.read_text().splitlines()[0] == f"chattr +C {data}"


def test_not_btrfs_means_no_chattr(tmp_path):
    bin_, log = _stubs(tmp_path, fs="ext2/ext3")
    assert _run(tmp_path, bin_, _code(tmp_path), tmp_path / "relay").returncode == 0
    assert not any(x.startswith("chattr") for x in log.read_text().splitlines())


@pytest.mark.parametrize("missing", ["code", "modules"])
def test_missing_code_or_modules_is_exit_78_not_a_restart_loop(tmp_path, missing):
    bin_, log = _stubs(tmp_path, modules_ok=(missing != "modules"))
    code = _code(tmp_path) if missing != "code" else tmp_path / "nowhere"
    out = _run(tmp_path, bin_, code, tmp_path / "relay")
    assert out.returncode == 78, (out.returncode, out.stderr)
    assert "RestartPreventExitStatus=78" in UNIT.read_text()


def test_the_unit_runs_the_launcher_before_the_graphical_session():
    unit = UNIT.read_text()
    assert "ExecStart=/usr/local/bin/pc-local-relay" in unit
    assert "Before=graphical-session-pre.target" in unit
    assert "WantedBy=default.target" in unit


def test_the_package_installs_both_enables_globally_and_the_session_depends_on_it():
    ebuilds = list(PKG.glob("posterchan-relay-*.ebuild"))
    assert len(ebuilds) == 1
    eb = ebuilds[0].read_text()
    for dep in ("app-misc/posterchan-server", "dev-python/numpy", "dev-python/websockets", "dev-python/httpx",
                "dev-libs/libsecp256k1"):
        assert dep in eb, dep
    assert 'doexe "${FILESDIR}/pc-local-relay"' in eb and "/usr/lib/systemd/user" in eb
    assert "systemctl --global enable posterchan-relay.service" in eb
    shell = next((ROOT / "os/overlay/app-misc/posterchanos-shell").glob("*.ebuild")).read_text()
    assert "app-misc/posterchan-relay" in shell
    assert (PKG / "files/pc-local-relay").read_bytes() == LAUNCHER.read_bytes()
    assert os.access(LAUNCHER, os.X_OK)
    pub = (ROOT / "scripts/publish_overlay.sh").read_text()
    assert 'bin/pc-local-relay" "$TMP/app-misc/posterchan-relay/files/pc-local-relay"' in pub
    assert "posterchan-relay-1.0.$(date" in pub
