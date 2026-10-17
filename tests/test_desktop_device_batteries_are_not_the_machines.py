"""A Bluetooth keyboard's battery is never shown as the machine's battery -- and IS shown on its Bluetooth row.

"our bluetooth is missing battery support? shows T keyboard with empty battery on taskbar" (2026-10-17): on the
TV (no battery of its own) the kernel's `hid-<address>-battery` supply, scope=Device, was taken for the system
battery. Runs the SHIPPED desktop/power.js against a fake sysfs, and desktop/bluetooth.js against a fake bluetoothctl.
"""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _supply(root, name, **files):
    d = root / "class" / "power_supply" / name
    d.mkdir(parents=True)
    for k, v in files.items():
        (d / k).write_text(str(v) + "\n")


def _battery(sysfs):
    js = "process.stdout.write(JSON.stringify(require(%r).battery()))" % str(ROOT / "desktop/power.js")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30,
                         env=dict(os.environ, PC_SYSFS=str(sysfs)))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_a_machine_with_only_a_keyboard_battery_has_no_battery(tmp_path):
    _supply(tmp_path, "hid-d1:01:09:03:96:ae-battery", type="Battery", scope="Device", capacity=38, status="Discharging")
    assert _battery(tmp_path) == {"present": False}


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_a_laptop_reports_its_own_battery_not_the_keyboards(tmp_path):
    _supply(tmp_path, "hid-d1:01:09:03:96:ae-battery", type="Battery", scope="Device", capacity=38, status="Discharging")
    _supply(tmp_path, "BAT0", type="Battery", capacity=81, status="Charging")
    b = _battery(tmp_path)
    assert b["present"] and b["name"] == "BAT0" and b["percent"] == 81, b


FAKE = r'''#!/bin/sh
case "$1" in
  show) printf '\tPowered: yes\n';;
  devices) printf 'Device D1:01:09:03:96:AE Bluetooth 5.1 Keyboard\n';;
  info) printf '\tName: Bluetooth 5.1 Keyboard\n\tIcon: input-keyboard\n\tPaired: yes\n\tConnected: yes\n\tBattery Percentage: 0x26 (38)\n';;
esac
'''


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_the_bluetooth_row_carries_the_devices_battery(tmp_path):
    fake = tmp_path / "btctl"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    js = "require(%r).status(false).then(s=>{process.stdout.write(JSON.stringify(s));process.exit(0);})" % str(ROOT / "desktop/bluetooth.js")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30,
                         env=dict(os.environ, PC_BLUETOOTHCTL=str(fake)))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["devices"][0]["battery"] == 38
    src = (ROOT / "static/js/client/osshell.js").read_text()
    row = src[src.index("function _btRow(x){"):src.index("function _btListHtml(")]
    assert "x.battery" in row and "%" in row, "the panel does not show a device's battery"
