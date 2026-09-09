"""A real clipboard-owner process must not retain an exited desktop's listener.

The executable fixture consumes the copy's actual stdin and forks like wl-copy.
No compositor, real clipboard, user processes, or live desktop are touched.

Run for IMAGES too, and that is the point of the parametrisation: image copy was added to this
bridge after the fd-closing wrapper was written, and a second spawn() of its own -- the obvious way
to add a second MIME type -- would have re-introduced the exact bug this file exists for, silently,
because a leaked descriptor breaks nothing until the desktop restarts and cannot rebind its port.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]

TEXT = 'Native copy: café 🌻\nsecond line; $(literal)'
# A minimal but real PNG: signature + a byte run that is NOT valid UTF-8, so a payload path that
# stringifies on its way to wl-copy corrupts it here rather than in somebody's paste.
PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(range(0x80, 0x90))

PAYLOADS = {
    'text': ('writeWaylandText', TEXT.encode(), 'text/plain'),
    'image': ('writeWaylandImage', PNG, 'image/png'),
}


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required')
@pytest.mark.parametrize('kind', sorted(PAYLOADS))
@pytest.mark.parametrize('descriptor', [9, 67])
def test_clipboard_owner_releases_inherited_socket_after_launcher_exit(tmp_path, descriptor, kind):
    fn, payload, mime = PAYLOADS[kind]
    report = tmp_path / 'owner.json'
    executable = tmp_path / 'wl copy;literal'
    executable.write_text('''#!/usr/bin/env python3
import base64, json, os, signal, sys
from pathlib import Path
copied = sys.stdin.buffer.read()
if os.fork():
    os._exit(0)
signal.alarm(20)
links = []
for name in os.listdir('/proc/self/fd'):
    try:
        links.append(os.readlink('/proc/self/fd/' + name))
    except FileNotFoundError:
        pass
report = Path(os.environ['PC_TEST_REPORT'])
pending = report.with_suffix('.tmp')
pending.write_text(json.dumps({
    'pid': os.getpid(), 'b64': base64.b64encode(copied).decode(), 'args': sys.argv[1:],
    'descriptors': links,
}))
pending.replace(report)
signal.pause()
''')
    executable.chmod(0o700)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    address = listener.getsockname()
    inherited = listener.fileno()
    identity = os.readlink(f'/proc/self/fd/{inherited}')
    js = r'''
const cp = require('child_process'), fs = require('fs');
const clipboard = require(MODULE);
clipboard[FN](PAYLOAD, {spawn(command, args, opts) {
  const stdio = opts.stdio.slice();
  while(stdio.length <= SLOT) stdio.push('ignore');
  stdio[SLOT] = FD;
  const child = cp.spawn(command, args, {...opts, stdio});
  fs.closeSync(FD);
  return child;
}}).then(ok => { process.stdout.write(JSON.stringify({ok})); process.exit(ok ? 0 : 2); });
'''.replace('MODULE', json.dumps(str(ROOT / 'desktop/clipboard.js'))).replace(
        'FN', json.dumps(fn)).replace(
        'PAYLOAD', json.dumps(TEXT) if kind == 'text'
        else 'Buffer.from(%s, "base64")' % json.dumps(base64.b64encode(payload).decode())).replace(
        'SLOT', str(descriptor)).replace('FD', str(inherited))
    owner = None
    try:
        result = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=8,
                                pass_fds=(inherited,), env={**os.environ,
                                'WAYLAND_DISPLAY': 'fixture-only', 'PC_WLCOPY': str(executable),
                                'PC_TEST_REPORT': str(report)})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {'ok': True}
        for _ in range(100):
            if report.exists():
                owner = json.loads(report.read_text())
                break
            time.sleep(.01)
        assert owner, 'clipboard owner did not report'
        # Node has now exited, just as the old Electron desktop had exited.
        listener.close()
        os.kill(owner['pid'], 0)
        assert base64.b64decode(owner['b64']) == payload
        assert owner['args'] == ['--type', mime]
        assert identity not in owner['descriptors'], owner
        with socket.socket() as replacement:
            replacement.bind(address)
            replacement.listen()
        os.kill(owner['pid'], 0)  # The clipboard remains alive after the port was reclaimed.
    finally:
        listener.close()
        if owner is None and report.exists():
            owner = json.loads(report.read_text())
        if owner:
            try:
                os.kill(owner['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
