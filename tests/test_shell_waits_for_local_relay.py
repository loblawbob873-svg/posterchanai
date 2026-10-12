"""pc-shell-start-wayfire waits for the local relay's socket -- bounded, and never as a refusal.

The `posterchan-relay` user unit writes $XDG_RUNTIME_DIR/posterchan-relay.url once its loopback socket is
listening (osmode.run). Waiting for it before the desktop starts means the first window's Store hydrates
from it. This RUNS the shipped block (`# >>> wait-for-local-relay`) from BOTH copies of the launcher under
sh, with a stub `systemctl` that records what it was asked and a URL file that appears on cue:
  * relay comes up after ~0.5 s       -> the launcher waits for it, then continues (and asked systemd to start it);
  * relay never comes up              -> the launcher gives up after its bound and CONTINUES (IndexedDB fallback);
  * a stale URL file, unit not active -> not taken as "listening" (a SIGKILLed relay leaves its file behind);
  * the unit is not installed         -> no wait at all (`systemctl cat` fails: nothing to wait for).
And the call sits before the launcher starts the shell.
"""
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = [ROOT / "os/bin/pc-shell-start-wayfire",
          ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-start-wayfire"]


def _block(path):
    m = re.search(r"# >>> wait-for-local-relay\n.*?# <<< wait-for-local-relay\n", path.read_text(), re.S)
    assert m, f"{path.name} does not wait for the local relay"
    return m.group(0)


def _stub(tmp, installed=True, active_flag=None):
    log = tmp / "systemctl.log"
    stub = tmp / "systemctl"
    active = f'[ -e "{active_flag}" ]' if active_flag else "true"
    stub.write_text(f"""#!/bin/sh
echo "$*" >>"{log}"
case "$*" in
  *" cat "*) {"exit 0" if installed else "echo 'No files found for posterchan-relay.service.' >&2; exit 1"} ;;
  *is-active*) {active} ;;
  *) exit 0 ;;
esac
""")
    stub.chmod(0o755)
    return stub, log


def _run(path, tmp, stub, ready, tenths=30, before=""):
    script = _block(path) + f"{before}\npc_wait_local_relay\necho CONTINUED\n"
    t0 = time.monotonic()
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30,
                         env={"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(tmp), "PC_SYSTEMCTL": str(stub),
                              "PC_RELAY_READY_FILE": str(ready), "PC_LOCAL_RELAY_WAIT_TENTHS": str(tenths)})
    return out, time.monotonic() - t0


@pytest.mark.parametrize("path", COPIES, ids=["os-bin", "overlay"])
def test_waits_for_the_socket_then_continues(path, tmp_path):
    ready = tmp_path / "posterchan-relay.url"
    stub, log = _stub(tmp_path)
    out, took = _run(path, tmp_path, stub, ready, tenths=50,
                     before=f"( sleep 0.6; echo ws://127.0.0.1:21001/ > {ready} ) &")
    assert out.returncode == 0 and out.stdout.rstrip().endswith("CONTINUED"), out
    assert "listening at ws://127.0.0.1:21001/" in out.stdout, out.stdout
    assert 0.5 <= took < 4.5, ("did not wait for the relay (or waited out the whole bound)", took)
    assert "--user start --no-block posterchan-relay.service" in log.read_text()


@pytest.mark.parametrize("path", COPIES, ids=["os-bin", "overlay"])
def test_a_relay_that_never_comes_up_costs_the_bound_and_the_desktop_still_starts(path, tmp_path):
    stub, _ = _stub(tmp_path)
    out, took = _run(path, tmp_path, stub, tmp_path / "never.url", tenths=15)
    assert out.returncode == 0 and out.stdout.rstrip().endswith("CONTINUED"), out
    assert 1.3 <= took < 4, took
    assert "IndexedDB" in out.stderr


@pytest.mark.parametrize("path", COPIES, ids=["os-bin", "overlay"])
def test_a_stale_url_file_is_not_a_listening_relay(path, tmp_path):
    ready = tmp_path / "posterchan-relay.url"
    ready.write_text("ws://127.0.0.1:21001/\n")
    stub, _ = _stub(tmp_path, active_flag=tmp_path / "active")
    out, took = _run(path, tmp_path, stub, ready, tenths=12)
    assert "listening at" not in out.stdout and took >= 1.0, (out, took)


@pytest.mark.parametrize("path", COPIES, ids=["os-bin", "overlay"])
def test_no_unit_installed_means_no_wait(path, tmp_path):
    stub, log = _stub(tmp_path, installed=False)
    out, took = _run(path, tmp_path, stub, tmp_path / "never.url", tenths=50)
    assert out.stdout.rstrip().endswith("CONTINUED") and took < 1.0, (out, took)
    assert "start" not in log.read_text()


@pytest.mark.parametrize("path", COPIES, ids=["os-bin", "overlay"])
def test_the_wait_comes_before_the_shell_starts(path):
    text = path.read_text()
    call = text.index("\npc_wait_local_relay\n")
    assert call < text.index('"$launcher" --shell'), "the desktop starts before the local relay is waited for"


def test_both_copies_are_the_same_file():
    assert COPIES[0].read_bytes() == COPIES[1].read_bytes()
