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


def _bt_status(tmp_path, info):
    fake = tmp_path / "btctl"
    fake.write_text("#!/bin/sh\ncase \"$1\" in\n  show) printf '\\tPowered: yes\\n';;\n"
                    "  devices) printf 'Device D1:01:09:03:96:AE Bluetooth 5.1 Keyboard\\n';;\n"
                    "  info) printf '%s';;\nesac\n" % info.replace("\n", "\\n").replace("\t", "\\t"))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    js = "require(%r).status(false).then(s=>{process.stdout.write(JSON.stringify(s));process.exit(0);})" % str(ROOT / "desktop/bluetooth.js")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30,
                         env=dict(os.environ, PC_BLUETOOTHCTL=str(fake), PC_SYSFS=str(tmp_path)))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["devices"][0]


BASE_INFO = "\tName: Bluetooth 5.1 Keyboard\n\tIcon: input-keyboard\n\tPaired: yes\n\tConnected: yes\n"


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_a_hid_keyboard_that_sends_percent_on_a_0_255_scale_reads_full_when_full(tmp_path):
    """The TV's keyboard, just charged (green LED): descriptor 0..255, kernel says 38 -- it is ~97%."""
    _supply(tmp_path, "hid-d1:01:09:03:96:ae-battery", type="Battery", scope="Device", capacity=38)
    dev = _bt_status(tmp_path, BASE_INFO + "\tModalias: usb:v045Ep005Fd011B\n")
    assert dev["battery"] == 97, dev


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_any_other_hid_keyboard_is_shown_as_the_kernel_reports_it(tmp_path):
    _supply(tmp_path, "hid-d1:01:09:03:96:ae-battery", type="Battery", scope="Device", capacity=38)
    assert _bt_status(tmp_path, BASE_INFO + "\tModalias: usb:v046DpB35Bd0001\n")["battery"] == 38


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_a_bluez_battery_reading_is_used_as_it_is(tmp_path):
    dev = _bt_status(tmp_path, BASE_INFO + "\tBattery Percentage: 0x26 (38)\n")
    assert dev["battery"] == 38, dev
    src = (ROOT / "static/js/client/osshell.js").read_text()
    row = src[src.index("function _btRow(x){"):src.index("function _btListHtml(")]
    assert "x.battery" in row and "%" in row, "the panel does not show a device's battery"
