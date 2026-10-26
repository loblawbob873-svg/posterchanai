"""The gate's and test.sh's test processes get no session D-Bus (scripts/private_tmp.py child_env).

Each headless Chrome a test starts otherwise registers with the desktop's xdg-desktop-portal, which keeps every
registration: on server1 it reached 29 GB in five days (2026-10-25) and the deploy watchdog stopped the gate for low
memory. Measured: with the bus a headless Chrome sends one Registry.Register; with `disabled:` it sends none.
"""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_child_env_turns_the_session_bus_off(tmp_path):
    pt = runpy.run_path(str(ROOT / "scripts/private_tmp.py"))
    env = pt["child_env"]({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus", "HOME": "/home/x"}, str(tmp_path))
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "disabled:", env.get("DBUS_SESSION_BUS_ADDRESS")
    assert env["TMPDIR"] == str(tmp_path) and env["HOME"] == "/home/x"


def test_every_gate_runner_builds_its_env_through_child_env():
    gate = (ROOT / "scripts/deploy_regression_gate.py").read_text()
    # the three runners (required+full, --suite, --changed); a new one that skips child_env would bring the bus back
    assert gate.count("private_tmp['child_env'](") >= 3
