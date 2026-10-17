"""A Bluetooth KEYBOARD can be paired from PosterChanOS: the pairing code reaches the screen.

"how am I supposed to connect my bluetooth keyboard!" (2026-10-17). Pairing ran `bluetoothctl pair MAC` as a
one-shot with no agent, so a keyboard -- which shows a passkey to type ON it, or asks the computer for a PIN --
could not be paired, and nothing on screen said why. desktop/bluetooth.js now pairs interactively with an agent
and hands every code to the caller. Runs the SHIPPED module against a fake bluetoothctl.
"""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE = r'''#!/usr/bin/env python3
import sys, time
mode = sys.argv[0].rsplit("-", 1)[-1]
log = open(%r, "a")
for line in sys.stdin:
    line = line.strip(); log.write(line + "\n"); log.flush()
    if line.startswith("pair "):
        if mode == "passkey":
            print("[agent] Passkey: 482913", flush=True); time.sleep(0.3)
            print("Pairing successful", flush=True)
        elif mode == "pin":
            sys.stdout.write("[agent] Enter PIN code: "); sys.stdout.flush()
        elif mode == "fail":
            print("Failed to pair: org.bluez.Error.AuthenticationFailed", flush=True)
    elif mode == "pin" and line.isdigit():
        print("Pairing successful", flush=True)
    elif line.startswith("connect "):
        print("Connection successful", flush=True)
    elif line == "quit":
        break
'''


def _pair(tmp_path, mode):
    log = tmp_path / "said.txt"
    fake = tmp_path / ("btctl-" + mode)
    fake.write_text(FAKE % str(log))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    js = """
const bt=require(%r); const codes=[];
bt.device('aa:bb:cc:dd:ee:01','pair',c=>codes.push(c)).then(r=>{process.stdout.write(JSON.stringify({r,codes}));process.exit(0);});
""" % str(ROOT / "desktop/bluetooth.js")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, PC_BLUETOOTHCTL=str(fake)))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout), log.read_text().splitlines()


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_a_keyboard_passkey_reaches_the_screen_and_it_pairs_trusts_and_connects(tmp_path):
    got, said = _pair(tmp_path, "passkey")
    assert got["codes"] == ["482913"], got
    assert got["r"]["ok"] is True, got
    assert said[:3] == ["agent KeyboardDisplay", "default-agent", "pair AA:BB:CC:DD:EE:01"], said
    assert "trust AA:BB:CC:DD:EE:01" in said and "connect AA:BB:CC:DD:EE:01" in said, said


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_an_older_keyboard_asking_for_a_pin_gets_one_and_it_is_shown(tmp_path):
    got, said = _pair(tmp_path, "pin")
    assert got["r"]["ok"] is True and len(got["codes"]) == 1 and len(got["codes"][0]) == 6, got
    assert got["codes"][0] in said, ("the PIN shown is not the PIN answered", got, said)


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_a_failed_pairing_says_so(tmp_path):
    got, _ = _pair(tmp_path, "fail")
    assert got["r"]["ok"] is False and "AuthenticationFailed" in got["r"]["error"], got


STATUS_FAKE = r'''#!/bin/sh
case "$1" in
  show) printf 'Controller 3C:78:95:9B:85:1D\n\tPowered: yes\n\tDiscovering: no\n';;
  devices) printf 'Device DC:2C:26:00:11:22 Arteck HB030B\nDevice 4B:63:A7:9F:65:30 4B-63-A7-9F-65-30\n';;
  info) if [ "$2" = "DC:2C:26:00:11:22" ]; then printf '\tName: Arteck HB030B\n\tIcon: input-keyboard\n\tPaired: no\n\tConnected: no\n';
        else printf '\tAlias: 4B-63-A7-9F-65-30\n\tPaired: no\n\tConnected: no\n'; fi;;
esac
'''


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_status_says_which_devices_really_have_a_name(tmp_path):
    """A device that never sent a name is listed by BlueZ under its address with dashes; the panel folds those."""
    fake = tmp_path / "btctl"
    fake.write_text(STATUS_FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    js = "require(%r).status(false).then(s=>{process.stdout.write(JSON.stringify(s));process.exit(0);})" % str(ROOT / "desktop/bluetooth.js")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30,
                         env=dict(os.environ, PC_BLUETOOTHCTL=str(fake)))
    assert out.returncode == 0, out.stderr
    rows = {d["address"]: d for d in json.loads(out.stdout)["devices"]}
    assert rows["DC:2C:26:00:11:22"]["named"] is True and rows["DC:2C:26:00:11:22"]["input"] is True, rows
    assert rows["4B:63:A7:9F:65:30"]["named"] is False, rows
