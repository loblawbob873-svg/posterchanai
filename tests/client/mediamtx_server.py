"""A PRIVATE MediaMTX for a test module: the release PosterChan ships, run by the app's OWN supervisor code.

scripts/install/stream.sh downloads a PINNED MediaMTX release (MEDIAMTX_VERSION, default below) into
<checkout>/streamserver/ and the `media` role runs it. A test must never touch that one (somebody may be live on it),
so this:

  obtains   the SAME release from the SAME url the installer builds (version read out of stream.sh, so a bump there
            moves here), ONCE -- cached under ~/.cache/posterchanai/test-tools/ and verified against the sha256 pinned
            below every time it is used. Nothing else in this module touches the network.
  runs      it through app/services/stream_service.py itself -- `_write_config` renders the config from the settings,
            `_spawn` starts it (PDEATHSIG, piped log, pidfile) and `_terminate` stops it -- with the module's
            streamserver/ paths pointed into a temp dir and every port a free one, so the generated config, the clamp
            script, the pidfile, the quality/state dirs and the recordings all live in that temp dir.
  stops     it with `_terminate`, then SIGKILLs anything still running out of the temp dir (a clamp ffmpeg that
            outlived it), removes the directory and ASSERTS nothing of it is left running.

$PC_MEDIAMTX_BIN points at a binary instead (it is still verified). `available()` is False when there is none and it
cannot be fetched (the desktop CI runner: no network for this, and these tests are not on its required list).
"""
from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / 'scripts' / 'install' / 'stream.sh'
CACHE = Path(os.environ.get('PC_TEST_TOOLS') or Path.home() / '.cache' / 'posterchanai' / 'test-tools')

# sha256 of the release TARBALL and of the `mediamtx` inside it, per (version, asset arch). Recorded from the first
# download of the pinned release; a different byte anywhere -- a re-tagged release, a truncated download, a cache
# file that was tampered with -- is refused rather than run.
PINNED = {
    ('v1.19.2', 'amd64'): ('f9c601cc303ceca8fad2883917b022882672c5bc56311e92dbceb16e5f20c60c',
                           '7dd8b247dac4105fa938fbae6222ebfe6e7889cdcfba219d65ea88144153c3c0'),
}


def version() -> str:
    """The MediaMTX version the installer ships (its `${MEDIAMTX_VERSION:-vX.Y.Z}` default)."""
    m = re.search(r'MEDIAMTX_VERSION:-(v[0-9][0-9A-Za-z.\-]*)\}', INSTALLER.read_text())
    assert m, 'scripts/install/stream.sh no longer pins MEDIAMTX_VERSION the way this reads it -- re-read it'
    return m.group(1)


def arch() -> str | None:
    return {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64',
            'armv7l': 'armv7', 'armv7': 'armv7'}.get(platform.machine())


def url(ver: str, a: str) -> str:
    # The installer's own formula: tarball="mediamtx_${ver}_${os}_${arch}.tar.gz", .../releases/download/${ver}/...
    return 'https://github.com/bluenviron/mediamtx/releases/download/%s/mediamtx_%s_linux_%s.tar.gz' % (ver, ver, a)


def _sha(path_or_bytes) -> str:
    h = hashlib.sha256()
    if isinstance(path_or_bytes, (bytes, bytearray)):
        h.update(path_or_bytes)
    else:
        with open(path_or_bytes, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
    return h.hexdigest()


_why = ''


def binary() -> Path | None:
    """The verified pinned binary, fetching it once if it is not cached. None (with `_why` set) when unobtainable."""
    global _why
    ver, a = version(), arch()
    pin = PINNED.get((ver, a or ''))
    env = os.environ.get('PC_MEDIAMTX_BIN')
    if env:
        p = Path(env)
        if not p.is_file():
            _why = '$PC_MEDIAMTX_BIN is not a file'
            return None
        if pin and _sha(p) != pin[1]:
            _why = '$PC_MEDIAMTX_BIN is not the pinned MediaMTX %s' % ver
            return None
        return p
    if not pin:
        _why = 'no pinned checksum for MediaMTX %s on %s' % (ver, platform.machine())
        return None
    dest = CACHE / ('mediamtx-%s-linux_%s' % (ver, a)) / 'mediamtx'
    if dest.is_file():
        if _sha(dest) == pin[1]:
            return dest
        dest.unlink()                                   # not what we pinned: fetch it again, never run it
    try:
        with urllib.request.urlopen(url(ver, a), timeout=120) as r:
            blob = r.read()
    except OSError as e:
        _why = 'could not download MediaMTX %s (%s)' % (ver, e)
        return None
    if _sha(blob) != pin[0]:
        _why = 'the downloaded MediaMTX %s tarball does not match its pinned sha256' % ver
        return None
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as tf:
        m = tf.getmember('mediamtx')
        assert m.isreg(), 'mediamtx in the release tarball is not a regular file'
        data = tf.extractfile(m).read()
    if _sha(data) != pin[1]:
        _why = 'the mediamtx inside the pinned tarball does not match its pinned sha256'
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.part')
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    os.replace(tmp, dest)
    return dest


_avail = None


def available() -> bool:
    global _avail
    if _avail is None:
        _avail = binary() is not None
    return _avail


def why() -> str:
    return _why or 'MediaMTX unavailable'


def free_port(udp=False) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def pids_under(path: str) -> list[int]:
    out = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
        except OSError:
            continue
        if path in cmd:
            out.append(int(p.name))
    return out


class MediaMTX:
    """`mp` is a pytest.MonkeyPatch the caller undoes; `app_port` is where MediaMTX's auth/unpublish hooks call (the
    test's instance, which hands /api/streams/* to the real router). `settings` override the stream_* defaults."""

    def __init__(self, mp, app_port: int, settings: dict | None = None):
        self.mp, self.app_port, self.extra = mp, app_port, dict(settings or {})
        self.tmp = None
        self.settings = {}

    def start(self, timeout=20):
        from app.services import settings_store, stream_service as ss
        b = binary()
        assert b, why()
        self.tmp = tempfile.mkdtemp(prefix='pc-test-mediamtx-')
        sd = Path(self.tmp, 'streamserver')
        sd.mkdir()
        (sd / 'mediamtx').symlink_to(b)
        for name, val in (('_STREAM_DIR', sd), ('_STREAM_BIN', sd / 'mediamtx'), ('_STREAM_CFG', sd / 'mediamtx.gen.yml'),
                          ('_CLAMP_SCRIPT', sd / 'clamp.sh'), ('_PIDFILE', sd / 'mediamtx.pid')):
            self.mp.setattr(ss, name, val)
        self.mp.setenv('POSTERCHANAI_PORT', str(self.app_port))
        self.rec = str(Path(self.tmp, 'rec'))
        self.settings = {
            'stream_enabled': 'true',
            'stream_rtmp_port': str(free_port()), 'stream_hls_port': str(free_port()),
            'stream_webrtc_port': str(free_port()), 'stream_webrtc_udp_port': str(free_port(udp=True)),
            'stream_api_port': str(free_port()), 'stream_rtsp_port': str(free_port()),
            'stream_srt_port': '', 'stream_domain': '127.0.0.1',
            'stream_auth_secret': secrets.token_hex(24),
            'stream_record_enabled': 'true', 'stream_record_dir': self.rec,
            # The clamp is ON by default on a node; here it runs on the CPU encoder so a test never takes the
            # GPU's media engine from the node it runs on.
            'stream_clamp_enabled': 'true', 'stream_clamp_encoder': 'libx264',
        }
        self.settings.update(self.extra)
        for k, v in self.settings.items():
            self.mp.setitem(settings_store._CACHE, k, v)
        self._spawn()
        self.wait_up(timeout)
        return self

    def _spawn(self):
        from app.services import settings_store, stream_service as ss
        ss._spawn(settings_store.all_settings())
        assert ss._proc is not None, 'stream_service._spawn did not start MediaMTX'
        self.proc = ss._proc

    def port(self, key):
        return int(self.settings['stream_%s_port' % key])

    def wait_up(self, timeout=20):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError('MediaMTX exited (%s); config:\n%s' % (self.proc.returncode, self.config()))
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/v3/paths/list' % self.port('api'), timeout=2) as r:
                    if r.status == 200:
                        return
            except OSError as e:
                last = e
            time.sleep(.1)
        raise RuntimeError('MediaMTX API never answered (%s)' % last)

    def config(self) -> str:
        try:
            return Path(self.tmp, 'streamserver', 'mediamtx.gen.yml').read_text()
        except OSError:
            return ''

    def path(self, name):
        """MediaMTX's own record of a path (its control API), or None."""
        import json
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/v3/paths/get/%s' % (self.port('api'), name), timeout=3) as r:
                return json.loads(r.read())
        except OSError:
            return None

    def down(self):
        """MediaMTX goes away (the app's own stop path), the directory stays."""
        from app.services import stream_service as ss
        ss._terminate()
        self._kill_rest()

    def up(self):
        self._spawn()
        self.wait_up()

    def _kill_rest(self):
        for _ in range(50):
            left = pids_under(self.tmp)
            if not left:
                return
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(.1)

    def stop(self):
        if self.tmp is None:
            return
        try:
            self.down()
        finally:
            left = pids_under(self.tmp)
            shutil.rmtree(self.tmp, ignore_errors=True)
            gone = not os.path.exists(self.tmp)
            tmp, self.tmp = self.tmp, None
        assert not left, ('MediaMTX (or its clamp) outlived stop()', left)
        assert gone, ('the MediaMTX temp dir is still there', tmp)
