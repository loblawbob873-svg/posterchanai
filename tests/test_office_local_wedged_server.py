"""A local office server that holds its port and never answers is given up on, not waited on for ever.

2026-10-13, the laptop: coolwsd's forkit died, coolwsd kept its port open with 3 connections unanswered, and the
desktop's readiness check -- a fetch with no timeout -- never returned. The start deadline was never reached and
every later open awaited the same promise: "Files -> click on CSV on this PC, nothing happens". This starts the
shipped OfficeLocal against a fake coolwsd that accepts connections and never answers, and requires the start to
FAIL within its deadline (and the next start to be a fresh attempt).

Run: venv-unified/bin/python -m pytest tests/test_office_local_wedged_server.py -q
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

WEDGED = """#!/usr/bin/env python3
import socket, sys, time
port = int([a.split('=', 1)[1] for a in sys.argv if a.startswith('--port=')][0])
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', port)); s.listen(3)
held = []
while True:
    c, _ = s.accept(); held.append(c)      # accepted, never answered
"""

JS = r"""
const { OfficeLocal } = require(process.env.MOD);
(async () => {
  const o = new OfficeLocal({ root: process.env.FAKE, startMs: 4000, workDir: process.env.WORK });
  const t0 = Date.now(); let err = '';
  try { await o._startCode(); } catch (e) { err = String(e && e.message || e); }
  const first = Date.now() - t0, cleared = o.code === null;
  try { o._stopCode(); } catch (_) {}
  process.stdout.write(JSON.stringify({ first, err, cleared }));
  process.exit(0);
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_wedged_office_server_is_given_up_on(tmp_path):
    fake = tmp_path / "root"
    (fake / "usr/bin").mkdir(parents=True)
    (fake / "etc/coolwsd").mkdir(parents=True)
    for name, body in (("coolwsd", WEDGED), ("coolwsd-systemplate-setup", "#!/bin/sh\nexit 0\n")):
        f = fake / "usr/bin" / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, MOD=str(ROOT / "desktop/office-local.js"), FAKE=str(fake), WORK=str(tmp_path / "work"))
    try:
        out = subprocess.run(["node", "-e", JS], capture_output=True, text=True, timeout=30, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail("starting against a wedged office server never gave up (no deadline on the readiness ask)")
    finally:
        subprocess.run(["pkill", "-f", str(fake / "usr/bin/coolwsd")], capture_output=True)
    import json
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert "did not start in time" in r["err"], r
    assert r["first"] < 10000, r
    assert r["cleared"], ("the wedged server is still the one the next open would wait on", r)



REPORTS_FDS = """#!/usr/bin/env python3
import json, os
out = os.environ['FDS_OUT']
open(out, 'w').write(json.dumps(sorted(int(f) for f in os.listdir('/proc/self/fd'))))
"""

# The argv OfficeLocal hands to spawn(), captured instead of run: plain node closes inherited descriptors itself,
# Electron does not -- so the leak is reproduced by running that exact argv from a parent that DOES leak one.
CAPTURE = r"""
const cp = require('child_process');
const { EventEmitter } = require('events');
cp.spawn = (cmd, args) => {
  if (String(cmd).endsWith('coolwsd-systemplate-setup')) {          // the template step: let it "finish"
    const e = new EventEmitter(); setImmediate(() => e.emit('close', 0)); return e;
  }
  process.stdout.write(JSON.stringify([cmd].concat(args))); process.exit(0);
};
const { OfficeLocal } = require(process.env.MOD);
new OfficeLocal({ root: process.env.FAKE, startMs: 4000, workDir: process.env.WORK })._startCode().catch(() => {});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_office_server_inherits_none_of_the_apps_descriptors(tmp_path):
    """Chromium's descriptors are not close-on-exec, and Electron's spawn does not close them: coolwsd inherited
    ~200 of the desktop's and handed them to coolforkit-ns, which exited 70 at birth -- Office never opened (laptop,
    2026-10-13). The command the app spawns must start the office server with nothing but stdio."""
    import json
    fake = tmp_path / "root"
    (fake / "usr/bin").mkdir(parents=True)
    (fake / "etc/coolwsd").mkdir(parents=True)
    for name, body in (("coolwsd", REPORTS_FDS), ("coolwsd-systemplate-setup", "#!/bin/sh\nexit 0\n")):
        f = fake / "usr/bin" / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, MOD=str(ROOT / "desktop/office-local.js"), FAKE=str(fake), WORK=str(tmp_path / "work"))
    got = subprocess.run(["node", "-e", CAPTURE], capture_output=True, text=True, timeout=30, env=env)
    argv = json.loads(got.stdout)
    assert argv[0].endswith("bash") or argv[0].endswith("coolwsd"), argv
    fds_out = tmp_path / "fds.json"
    leak = os.open(str(tmp_path / "leaked"), os.O_CREAT | os.O_RDWR)
    try:
        subprocess.run(argv, env=dict(env, FDS_OUT=str(fds_out)), pass_fds=(leak,), timeout=30,
                       stdout=subprocess.DEVNULL)
    finally:
        os.close(leak)
    fds = json.loads(fds_out.read_text())
    assert leak not in fds, ("the office server inherited the app's descriptors", fds, leak)
