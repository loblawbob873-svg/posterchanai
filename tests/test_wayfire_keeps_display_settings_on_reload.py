"""A config reload must not undo the displays System Settings chose.

Reported 2026-09-24: "keep alive: on on TV PC causes the desktop to change the scale". Keep Awake (pc-idle
hold) changes the idle timers with `wayfire/set-config-options`, and Wayfire's handler for that ALWAYS
ends in `reload_config_signal` (plugins/ipc-rules/ipc-utility-methods.hpp), on which the output layout
runs `reconfigure_from_config()` -- re-applying every [output:*] from wayfire.ini. System Settings
applies scale/position/mode live through wlr-output-management and never writes wayfire.ini, so the
reload put the TV back to the default scale. The same happened on a display-timeout change and on
pointer confinement for a fullscreen game (pc-pointer-confine uses the same call).

Wayfire's own switch for this -- `workarounds/use_external_output_configuration` -- makes it leave an
output alone once a client has configured it (`should_ignore_config_state`). Default false.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini"


def _section(text: str, name: str) -> dict:
    out, cur = {}, None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            continue
        if cur == name and "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_the_shipped_config_lets_the_desktops_display_settings_stand():
    assert _section(SHIPPED.read_text(), "workarounds").get("use_external_output_configuration") == "true", \
        "a config reload (Keep Awake, the display timeout, pointer confinement) resets the display scale"


def test_the_session_config_the_compositor_actually_reads_keeps_it(tmp_path):
    """pc-compositor-session hands wayfire a GENERATED copy; a setting it drops never reaches the TV."""
    src = tmp_path / "wayfire.ini"
    src.write_text(SHIPPED.read_text())
    body = (ROOT / "os/bin/pc-compositor-session").read_text() \
        .split("<<'PC_SESSION_CONFIG_PY'\n", 1)[1].split("\nPC_SESSION_CONFIG_PY", 1)[0]
    out = tmp_path / "runtime.ini"
    r = subprocess.run([sys.executable, "-c", body, str(src), str(out), ""], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert _section(out.read_text(), "workarounds").get("use_external_output_configuration") == "true"
