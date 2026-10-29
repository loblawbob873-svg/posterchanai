"""desktop/anticheat.js -- the System Settings bridge to pc-anticheat -- asks the helper the right question.

Run under node with PC_SUDO pointing at a stub that records its argv and answers like the helper: travel mode goes out
as `sudo -n pc-anticheat travel on|off` and comes back as the fresh status, and an ordinary account's refusal is said
in words. The IPC names are the ones preload exposes.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _run(tmp_path, js, sudo_body):
    sudo = tmp_path / "sudo"
    sudo.write_text("#!/bin/sh\necho \"$*\" >> %s\n%s\n" % (tmp_path / "argv", sudo_body))
    sudo.chmod(0o755)
    env = dict(os.environ, PC_SUDO=str(sudo), PC_ANTICHEAT_HELPER="/usr/local/bin/pc-anticheat")
    code = "const a=require(%s);(async()=>{try{%s}catch(e){console.log(JSON.stringify({err:e.message}));}})();" % (
        json.dumps(str(ROOT / "desktop/anticheat.js")), js)
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    argv = (tmp_path / "argv").read_text().splitlines() if (tmp_path / "argv").exists() else []
    return json.loads(r.stdout.strip().splitlines()[-1]), argv


@pytest.mark.skipif(not NODE, reason="node required")
@pytest.mark.parametrize("on,word", [(True, "on"), (False, "off")])
def test_travel_asks_the_helper_and_returns_the_new_status(tmp_path, on, word):
    out, argv = _run(tmp_path, "console.log(JSON.stringify(await a.travel(%s)));" % ("true" if on else "false"),
                     'case "$*" in *status*) echo \'{"state":"enabled","travel":%s}\';; esac' % ("true" if on else "false"))
    assert argv == [f"-n /usr/local/bin/pc-anticheat travel {word}", "-n /usr/local/bin/pc-anticheat status"], argv
    assert out == {"state": "enabled", "travel": on}


@pytest.mark.skipif(not NODE, reason="node required")
def test_an_ordinary_account_is_told_in_words(tmp_path):
    out, _ = _run(tmp_path, "await a.travel(true);", 'echo "sudo: a password is required" >&2; exit 1')
    assert out == {"err": "Only the administrator of this computer can change Secure Boot."}


def test_the_ipc_reaches_it():
    main = (ROOT / "desktop/main.js").read_text(); pre = (ROOT / "desktop/preload.js").read_text()
    assert "ipcMain.handle('pc:anticheat:travel', (e, on) => { fsGuard(e); return anticheat.travel(on === true); });" in main
    assert "travel: (on) => ipcRenderer.invoke('pc:anticheat:travel', on === true)," in pre
