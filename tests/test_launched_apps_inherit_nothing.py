"""An app started from PosterChanOS inherits none of the desktop's descriptors.

Measured 2026-10-16 on .102: Steam, launched from the desktop hours earlier, owned the desktop's remote-debugging
LISTENER (port 9222). Chromium does not mark its descriptors close-on-exec and spawn() passes them on, so every
program started from the start menu, a .desktop launcher, xdg-open or a VM viewer held the desktop's sockets and
files; after a shell restart the new desktop could not bind its own port. desktop/cleanspawn.js closes every
descriptor above stderr and execs the program (same PID -- windows are matched by PID).
"""
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
PROBE = 'echo PID $$; for f in /proc/$$/fd/*; do readlink $f; done'


def _clean_argv(argv):
    js = "console.log(JSON.stringify(require(%r).cleanArgv(JSON.parse(process.argv[1]))))" % str(ROOT / "desktop/cleanspawn.js")
    return json.loads(subprocess.check_output([NODE, "-e", js, json.dumps(argv)], text=True))


@pytest.mark.skipif(not NODE, reason="node required")
def test_a_launched_program_gets_no_inherited_socket_and_keeps_its_pid():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    os.set_inheritable(s.fileno(), True)          # what Chromium's listener is: not close-on-exec
    try:
        plain = subprocess.run(["/bin/sh", "-c", PROBE], pass_fds=(s.fileno(),), capture_output=True, text=True).stdout
        assert any("socket:" in x for x in plain.split()), "the probe cannot see an inherited socket at all"
        p = subprocess.Popen(_clean_argv(["/bin/sh", "-c", PROBE]), pass_fds=(s.fileno(),),
                             stdout=subprocess.PIPE, text=True)
        out = p.communicate(timeout=30)[0]
        assert not [x for x in out.split() if "socket:" in x], ("the launched program inherited the desktop's socket", out)
        assert ("PID %d" % p.pid) in out, "the app must keep the PID it was started with (windows are matched by PID)"
    finally:
        s.close()
    r = subprocess.run(_clean_argv(["pc-no-such-program-xyz"]), capture_output=True)
    assert r.returncode == 127


RECORD = r"""
const cp=require('child_process'); const seen=[]; const real=cp.spawn;
cp.spawn=(cmd,args,opts)=>{ seen.push([cmd].concat(args||[])); return real('/bin/true',[],{stdio:'ignore'}); };
const out={};
const {SwayWM:WM}=require(%(root)r+'/desktop/wm.js'); WM.prototype.launch.call(Object.create(WM.prototype),['firefox','--new-window']);
out.wm=seen.pop();
const W=require(%(root)r+'/desktop/wm-wayfire.js').WayfireWM; W.prototype.launch.call(Object.create(W.prototype),['steam']);
out.wayfire=seen.pop();
const hf=require(%(root)r+'/desktop/hostfs.js'); try{ hf.open(%(file)r); }catch(e){ out.hostfsErr=String(e); }
out.hostfs=seen.pop();
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not NODE, reason="node required")
def test_every_launch_path_goes_through_the_wrapper(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    got = json.loads(subprocess.check_output([NODE, "-e", RECORD % {"root": str(ROOT), "file": str(f)}],
                                             text=True, cwd=str(ROOT)).strip().splitlines()[-1])
    close = json.loads(subprocess.check_output([NODE, "-e", "console.log(JSON.stringify(require(%r).CLOSE_AND_EXEC))"
                                                % str(ROOT / "desktop/cleanspawn.js")], text=True))
    for name, prog in (("wm", "firefox"), ("wayfire", "steam"), ("hostfs", "xdg-open")):
        argv = got.get(name) or []
        assert argv[:3] == ["/bin/sh", "-c", close] and prog in argv, (name, argv, got.get("hostfsErr"))
