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
