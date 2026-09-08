"""Exercise the real terminal process tree with an inherited listening socket."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which('node') or not shutil.which('script'), reason='requires Node and util-linux script')
@pytest.mark.parametrize('descriptor,literal_shell_path', [(9, False), (64, False), (64, True)])
def test_script_and_shell_release_inherited_listener_but_keep_terminal_io(descriptor, literal_shell_path, tmp_path):
    # Inject a real inherited FD at the spawn boundary, reproducing Chromium's
    # non-CLOEXEC descriptors without Electron or a user's running terminal.
    shell = Path('/bin/bash')
    if literal_shell_path:
        shell = tmp_path / 'terminal shell;literal'
        shell.symlink_to('/bin/bash')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    address = listener.getsockname()
    inherited = listener.fileno()
    identity = os.readlink(f'/proc/self/fd/{inherited}')
    js = r'''
const cp = require('child_process'), fs = require('fs');
const spawn = cp.spawn;
let child;
cp.spawn = (command, args, opts) => {
  const stdio = opts.stdio.slice();
  while(stdio.length <= SLOT) stdio.push('ignore');
  stdio[SLOT] = FD;
  child = spawn(command, args, {...opts, stdio});
  return child;
};
const T = require(MODULE);
const session = T.start({cols: 113, rows: 37});
fs.closeSync(FD);
let output = '', reported = false;
const deadline = setTimeout(() => { T.closeAll(); process.exit(2); }, 10000);
function links(pid) {
  return fs.readdirSync(`/proc/${pid}/fd`).flatMap(n => {
    try { return [fs.readlinkSync(`/proc/${pid}/fd/${n}`)]; } catch (_) { return []; }
  });
}
T.subscribe(session.id, ev => {
  if(ev.t !== 'out') return;
  output += ev.d;
  const pid = /SHELL_PID=(\d+)/.exec(output);
  if(!reported && pid && output.includes('OUTPUT_OK') && output.includes('ERROR_OK') && output.includes('SIZE=37 113')) {
    reported = true;
    process.stdout.write(JSON.stringify({parent: child.pid, shell: +pid[1],
      parentName: fs.readFileSync(`/proc/${child.pid}/comm`, 'utf8').trim(),
      parentFds: links(child.pid), shellFds: links(+pid[1]), output,
      alive: T.backlog(session.id, 0).alive}) + '\n');
  }
});
// Disable echo first, so commands themselves cannot satisfy output assertions.
setTimeout(() => T.write(session.id, 'stty -echo\n'), 150);
setTimeout(() => T.write(session.id, 'printf "SHELL_PID=%s\\n" "$$"; printf "OUTPUT_%s\\n" OK; printf "ERROR_%s\\n" OK >&2; printf "SIZE="; stty size\n'), 300);
process.stdin.on('data', () => {
  T.write(session.id, 'printf "STILL_%s\\n" RUNNING\n');
  const end = setInterval(() => {
    if(output.includes('STILL_RUNNING')) {
      clearInterval(end); clearTimeout(deadline);
      process.stdout.write(JSON.stringify({continued: true}) + '\n');
      T.closeAll(); setTimeout(() => process.exit(0), 100);
    }
  }, 20);
});
'''.replace('MODULE', json.dumps(str(ROOT / 'desktop/localterm.js'))).replace('SLOT', str(descriptor)).replace('FD', str(inherited))
    proc = subprocess.Popen(['node', '-e', js], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, pass_fds=(inherited,),
                            env={**os.environ, 'SHELL': str(shell)})
    try:
        line = proc.stdout.readline()
        assert line, proc.stderr.read()
        result = json.loads(line)
        listener.close()
        assert result['parentName'] == 'script', result
        assert result['parent'] != result['shell'], result
        assert identity not in result['parentFds'], result
        assert identity not in result['shellFds'], result
        assert result['alive'], result
        # Both original owners released it; a still-open terminal must not hold
        # the port hostage when the desktop tries to restart its listener.
        with socket.socket() as replacement:
            replacement.bind(address)
            replacement.listen()
        remaining, errors = proc.communicate('continue\n', timeout=12)
        assert proc.returncode == 0, errors
        assert json.loads(remaining)['continued'] is True
    finally:
        listener.close()
        if proc.poll() is None:
            try:
                proc.communicate('stop\n', timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
