"""A PRIVATE Collabora Online (CODE) for a test module: the AppImage PosterChan ships, never the node's own editor.

scripts/install/office.sh downloads `Collabora_Online.AppImage` into <checkout>/officeserver/ and the node runs it as
posterchanai-office.service on :9983. A test must never touch that one (somebody may have a document open in it) and
cannot simply start a second copy the same way: the AppImage's own AppRun hard-codes `--port=9983`, and coolwsd
calls a repeated option fatal. So this unpacks the SAME AppImage into a temporary directory, runs the SAME AppRun with
only the port changed, with HOME/TMPDIR inside that directory, and trusts exactly one WOPI host (the test's
instance) through `storage.wopi.alias_groups` -- the setting a real deployment uses to name its storage host.

`stop()` kills the whole process group, then anything still running out of the temporary directory (a kit that
outlived its parent), removes the directory and ASSERTS that nothing of it is left running.

Where the AppImage is: $PC_CODE_APPIMAGE, else <checkout>/officeserver/Collabora_Online.AppImage (what a node has).
None -> `available()` is False and the tests that need it skip. They are NOT on the CI's required list.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = '/office-code'          # app/routers/office.py's _SERVICE_ROOT; the unit starts CODE with it too


def appimage() -> Path | None:
    for cand in (os.environ.get('PC_CODE_APPIMAGE'), ROOT / 'officeserver' / 'Collabora_Online.AppImage'):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return Path(cand)
    return None


def available() -> bool:
    return appimage() is not None


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pids_under(path: str) -> list[int]:
    """Every process whose command line or executable lives under `path` -- what a CODE of ours would be."""
    out = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
        except OSError:
            continue
        exe = ''
        try:
            exe = os.readlink(p / 'exe')
        except OSError:
            pass
        if path in cmd or exe.startswith(path):
            out.append(int(p.name))
    return out


class CodeServer:
    def __init__(self, wopi_host: str, extra: tuple[str, ...] = ()):
        """`wopi_host`: the one storage host CODE may load documents from, e.g. http://127.0.0.1:41234."""
        self.wopi_host, self.extra = wopi_host, extra
        self.proc = None
        self.tmp = None
        self.port = None
        self.started_in = None
        self.log = None

    @property
    def url(self) -> str:
        return 'http://127.0.0.1:%d' % self.port

    def start(self, timeout=120):
        image = appimage()
        assert image, 'no Collabora_Online.AppImage (set PC_CODE_APPIMAGE)'
        t0 = time.monotonic()
        self.tmp = tempfile.mkdtemp(prefix='pc-test-code-')
        home, work = Path(self.tmp, 'home'), Path(self.tmp, 'tmp')
        home.mkdir()
        work.mkdir()
        env = {**os.environ, 'HOME': str(home), 'TMPDIR': str(work)}
        # Unpack it (read-only use of the shipped file; ~3 s, ~830 MB in the temp dir).
        subprocess.run([str(image), '--appimage-extract'], cwd=self.tmp, env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        squash = Path(self.tmp, 'squashfs-root')
        apprun = (squash / 'AppRun').read_text()
        assert apprun.count('--port=9983') == 1, 'the AppImage AppRun changed shape -- re-read it'
        self.port = _free_port()
        mine = squash / 'AppRun.test'
        mine.write_text(apprun.replace('--port=9983', '--port=%d' % self.port))
        mine.chmod(0o755)
        args = [str(mine),
                # What posterchanai-office.service passes, minus the public bind: loopback, IPv4, the sub-path.
                '--o:net.proto=IPv4', '--o:net.listen=loopback', '--o:welcome.enable=false',
                '--o:net.service_root=' + SERVICE_ROOT,
                # Trust ONE storage host: the test's instance. Anything else asking CODE to load a document is refused.
                '--o:storage.wopi.alias_groups[@mode]=groups',
                '--o:storage.wopi.alias_groups.group[0].host=' + self.wopi_host,
                '--o:storage.wopi.alias_groups.group[0].host[@allow]=true',
                '--o:num_prespawn_children=1', '--o:admin_console.enable=false', *self.extra]
        self.log = open(Path(self.tmp, 'code.log'), 'wb')
        self.proc = subprocess.Popen(args, cwd=self.tmp, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError('CODE exited (%s): %s' % (self.proc.returncode, self.tail()))
            try:
                with urllib.request.urlopen(self.url + SERVICE_ROOT + '/hosting/discovery', timeout=2) as r:
                    if r.status == 200 and b'urlsrc' in r.read():
                        self.started_in = time.monotonic() - t0
                        return self
            except OSError as e:
                last = e
            time.sleep(.2)
        raise RuntimeError('CODE did not answer discovery in %ss (%s): %s' % (timeout, last, self.tail()))

    def tail(self, n=4000) -> str:
        try:
            return Path(self.tmp, 'code.log').read_bytes()[-n:].decode('utf-8', 'replace')
        except OSError:
            return ''

    def kill(self):
        """CODE goes down, all of it, the way a crash or `systemctl stop` takes it -- the directory stays."""
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for _ in range(50):
            left = _pids_under(self.tmp)
            if not left:
                break
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(.1)

    def stop(self):
        if self.tmp is None:
            return
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        self.kill()
        left = _pids_under(self.tmp)
        try:
            self.log.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        gone = not os.path.exists(self.tmp)
        tmp, self.tmp = self.tmp, None
        assert not left, ('CODE processes outlived stop()', left)
        assert gone, ('the CODE temp dir is still there', tmp)
