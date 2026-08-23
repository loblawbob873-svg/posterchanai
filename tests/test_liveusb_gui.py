"""The LiveUSB GUI must never offer the system disk or silently overwrite mounted media."""
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
UI = (ROOT / "static/js/client/os.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()
MAIN = (ROOT / "desktop/main.js").read_text()


def test_settings_exposes_build_pick_burn_and_confirmation():
    for marker in ("pcLiveUSB.build", "pcLiveUSB.burn", "pcLiveUSB.pickISO", "pcLiveUSB.pickDir"):
        assert marker in UI
    assert "Everything on "+"'" in UI
    assert "Erase and write USB" in UI
    assert "pc:liveusb:burn" in PRELOAD and "pc:liveusb:burn" in MAIN


def test_device_scan_excludes_system_disk_and_marks_mounted_usb(tmp_path):
    fake = tmp_path / "lsblk"
    data = {"blockdevices": [
        {"name":"nvme0n1","path":"/dev/nvme0n1","type":"disk","size":1000,"rm":0,"tran":"nvme","mountpoints":[None],"children":[
            {"name":"nvme0n1p2","path":"/dev/nvme0n1p2","type":"part","size":900,"rm":0,"mountpoints":["/"]}]},
        {"name":"sdb","path":"/dev/sdb","type":"disk","size":2000,"rm":1,"tran":"usb","model":"SAFE USB","mountpoints":[None],"children":[]},
        {"name":"sdc","path":"/dev/sdc","type":"disk","size":3000,"rm":1,"tran":"usb","model":"MOUNTED","mountpoints":[None],"children":[
            {"name":"sdc1","path":"/dev/sdc1","type":"part","size":2900,"rm":1,"mountpoints":["/media/x"]}]},
    ]}
    fake.write_text("#!/bin/sh\nprintf '%s' '" + json.dumps(data) + "'\n")
    fake.chmod(0o755)
    env = {**os.environ, "PC_LSBLK": str(fake)}
    js = "require('./desktop/liveusb').devices().then(x=>process.stdout.write(JSON.stringify(x)))"
    got = json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, env=env))
    assert [x["path"] for x in got] == ["/dev/sdb", "/dev/sdc"]
    assert got[0]["mounted"] is False
    assert got[1]["mounted"] is True


def test_backend_uses_argument_arrays_not_a_shell():
    src = (ROOT / "desktop/liveusb.js").read_text()
    assert "shell:true" not in src.replace(" ", "")
    assert "another LiveUSB job is already running" in src
    assert "the selected removable disk is no longer available" in src
    assert "of='+target" in src

