"""A real clipboard-owner process must not retain an exited desktop's listener.

The executable fixture consumes the copy's actual stdin and forks like wl-copy.
No compositor, real clipboard, user processes, or live desktop are touched.
"""
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


@pytest.mark.skipif(not shutil.which('node'), reason='Node is required')
@pytest.mark.parametrize('descriptor', [9, 67])
def test_clipboard_owner_releases_inherited_socket_after_launcher_exit(tmp_path, descriptor):
    report = tmp_path / 'owner.json'
    executable = tmp_path / 'wl copy;literal'
    executable.write_text('''#!/usr/bin/env python3
import json, os, signal, sys
from pathlib import Path
copied = sys.stdin.read()
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
    'pid': os.getpid(), 'text': copied, 'args': sys.argv[1:], 'descriptors': links,
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
    text = 'Native copy: café 🌻\nsecond line; $(literal)'
    js = r'''
const cp = require('child_process'), fs = require('fs');
const clipboard = require(MODULE);
clipboard.writeWaylandText(TEXT, {spawn(command, args, opts) {
  const stdio = opts.stdio.slice();
  while(stdio.length <= SLOT) stdio.push('ignore');
  stdio[SLOT] = FD;
  const child = cp.spawn(command, args, {...opts, stdio});
  fs.closeSync(FD);
  return child;
}}).then(ok => { process.stdout.write(JSON.stringify({ok})); process.exit(ok ? 0 : 2); });
'''.replace('MODULE', json.dumps(str(ROOT / 'desktop/clipboard.js'))).replace(
        'TEXT', json.dumps(text)).replace('SLOT', str(descriptor)).replace('FD', str(inherited))
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
        assert owner['text'] == text
        assert owner['args'] == ['--type', 'text/plain']
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
