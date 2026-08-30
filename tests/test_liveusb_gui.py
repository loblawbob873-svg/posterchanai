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


def test_built_iso_path_flows_straight_into_the_usb_writer():
    """Building and writing are adjacent steps, not two unrelated file pickers."""
    assert "{path:image}" in (ROOT / "desktop/liveusb.js").read_text()
    assert "if(s&&s.path)setIso(s.path)" in UI
    assert "if(s.kind==='build'&&s.path&&(s.running||s.ok))setIso(s.path)" in UI


def test_running_build_is_authoritative_and_cannot_be_double_started():
    assert "buildButton.disabled=!!(s.running||s.launching)" in UI
    assert "active&&active.kind==='build'&&(active.running||active.launching)" in UI
    assert "never tell the user a live" in UI


def test_backend_returns_the_exact_iso_path_when_build_starts(tmp_path):
    sudo = tmp_path / "sudo"
    sudo.write_text("#!/bin/sh\nexit 0\n")
    sudo.chmod(0o755)
    out = tmp_path / "images"
    out.mkdir()
    js = "process.stdout.write(JSON.stringify(require('./desktop/liveusb').build(process.argv[1],false)))"
    env = {**os.environ, "PC_SUDO": str(sudo), "PC_LIVEUSB_STATE_DIR": str(tmp_path / "state")}
    got = json.loads(subprocess.check_output(["node", "-e", js, str(out)], cwd=ROOT, env=env))
    assert Path(got["path"]).parent == out
    assert Path(got["path"]).name.startswith("posterchan-live-")
    assert Path(got["path"]).suffix == ".iso"


def test_gui_build_cannot_publish_before_runtime_release_gates(tmp_path):
    """Building in Settings creates a candidate; it must not replace the public ISO."""
    sudo, invoked = tmp_path / "sudo", tmp_path / "invoked"
    sudo.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PC_TEST_ARGS\"\n")
    sudo.chmod(0o755)
    out = tmp_path / "images"
    out.mkdir()
    js = "require('./desktop/liveusb').build(process.argv[1],false)"
    env = {**os.environ, "PC_SUDO": str(sudo), "PC_TEST_ARGS": str(invoked),
           "PC_LIVEUSB_STATE_DIR": str(tmp_path / "state")}
    subprocess.check_call(["node", "-e", js, str(out)], cwd=ROOT, env=env)
    import time
    deadline = time.time() + 3
    while not invoked.exists() and time.time() < deadline:
        time.sleep(.02)
    args = invoked.read_text().splitlines()
    assert "PC_ISO_CLEAN=y" in args
    assert "PC_ISO_PUBLISH=n" in args
    assert args.index("PC_ISO_PUBLISH=n") < args.index("/usr/bin/gentoo.sh")


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


def test_iso_build_survives_the_desktop_process_and_recovers_status():
    src = (ROOT / "desktop/liveusb.js").read_text()
    assert "detached:true" in src and "p.unref()" in src
    assert "liveusb-job.json" in src and "liveusb-job.log" in src
    assert "recover();" in src
    runner = (ROOT / "desktop/liveusb-runner.js").read_text()
    assert "stdio:['ignore',fd,fd]" in runner


def _wait_status(state, timeout=5):
    import time
    js = "process.stdout.write(JSON.stringify(require('./desktop/liveusb').status()))"
    env = {**os.environ, "PC_LIVEUSB_STATE_DIR": str(state)}
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, env=env))
        if not got.get("running") and not got.get("launching") and got.get("finished"):
            return got
        time.sleep(.05)
    raise AssertionError("detached LiveUSB supervisor did not record a terminal state")


def test_fresh_process_recovers_real_supervisor_exit_status(tmp_path):
    sudo = tmp_path / "sudo"
    sudo.write_text("#!/bin/sh\nsleep .2\nexit 7\n")
    sudo.chmod(0o755)
    out, state = tmp_path / "images", tmp_path / "state"
    out.mkdir()
    # A stale artifact must never turn a failed new build into success.
    from datetime import datetime
    (out / f"posterchan-live-{datetime.now():%Y%m%d}.iso").write_bytes(b"partial")
    env = {**os.environ, "PC_SUDO": str(sudo), "PC_LIVEUSB_STATE_DIR": str(state)}
    subprocess.check_call(["node", "-e", "require('./desktop/liveusb').build(process.argv[1],false)", str(out)], cwd=ROOT, env=env)
    got = _wait_status(state)
    assert got["ok"] is False and got["exitCode"] == 7
    assert "exit 7" in got["message"]


def test_detached_supervisor_records_fast_success(tmp_path):
    sudo = tmp_path / "sudo"
    sudo.write_text("#!/bin/sh\nexit 0\n")
    sudo.chmod(0o755)
    out, state = tmp_path / "images", tmp_path / "state"
    out.mkdir()
    env = {**os.environ, "PC_SUDO": str(sudo), "PC_LIVEUSB_STATE_DIR": str(state)}
    subprocess.check_call(["node", "-e", "require('./desktop/liveusb').build(process.argv[1],false)", str(out)], cwd=ROOT, env=env)
    got = _wait_status(state)
    assert got["ok"] is True and got["exitCode"] == 0


def test_pid_identity_rejects_reused_process(tmp_path):
    js = "process.stdout.write(String(require('./desktop/liveusb')._alive({pid:process.pid,procStart:'definitely-wrong',token:'no-such-token'})))"
    assert subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True) == "false"
