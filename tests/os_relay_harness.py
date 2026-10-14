"""Shared fixtures for PosterChanOS's local relay (app/services/nostr_relay/osmode.py).

Keys, signed events, a real instance (a RelayServer over its own PosterChanDB store, plus the HTTP
/client/config the desktop's chosen instance answers) and a launcher for the SHIPPED relay process
(`relay_main.py --os-mode`), with an optional socket recorder injected into that process.
"""
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.nostr.event import build_event  # noqa: E402

OWNER_SK = bytes([1] * 32)
FRIEND_SK = bytes([2] * 32)
STRANGER_SK = bytes([3] * 32)


def pk(sk: bytes) -> str:
    return build_event(sk, 1, "x")["pubkey"]


def ev(sk, kind=1, content="hello", tags=None, created_at=None):
    return build_event(sk, kind, content, tags or [], created_at)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------------------------------- the instance
class Instance:
    """A real relay (the server role's RelayServer + PcdbRelayStore, so NEG/AUTH/NIP-78 behave exactly as on
    a node) on 127.0.0.1, plus the instance's HTTP `/client/config` naming it. Runs in its own thread/loop."""

    def __init__(self, tmp: str, neg_max_items: int = 50000):
        self.tmp = tmp
        self.neg_max_items = neg_max_items
        self.ws_port = free_port()
        self.relay_url = "ws://127.0.0.1:%d/" % self.ws_port
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.http = None
        self.stop_ev = None

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        from websockets.asyncio.server import serve
        from app.services.nostr_relay.pcdb_store import PcdbRelayStore, init_new
        from app.services.nostr_relay.server import RelayServer
        from app.services.nostr_relay.wot import WotGate
        path = os.path.join(self.tmp, "instance")
        init_new(path)
        self.store = PcdbRelayStore(path, max_events=0, retention_days=0, maintenance=False)
        self.store.open(asyncio.get_running_loop())
        self.server = RelayServer(self.store, WotGate(), {"wot_enabled": False, "block_json": False,
                                                         "max_subs_per_conn": 64, "neg_max_items": self.neg_max_items})
        self.stop_ev = asyncio.Event()
        async with serve(self.server.handle, "127.0.0.1", self.ws_port,
                         process_request=self.server.process_request):
            self.ready.set()
            await self.stop_ev.wait()
        self.store.close()

    def start(self):
        self.thread.start()
        assert self.ready.wait(20), "instance relay did not start"
        relay_url = self.relay_url

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/client/config"):
                    body = json.dumps({"relay_url": relay_url, "nostr_only": True}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                else:
                    body = b"{}"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.http_port = self.http.server_port
        self.url = "http://127.0.0.1:%d" % self.http_port
        return self

    def add(self, events):
        fut = asyncio.run_coroutine_threadsafe(self.store.add_events_bulk(list(events), origin="direct"), self.loop)
        return fut.result(30)

    def close(self):
        if self.http:
            self.http.shutdown()
            self.http.server_close()
        if self.stop_ev is not None:
            self.loop.call_soon_threadsafe(self.stop_ev.set)
        self.thread.join(10)


# ------------------------------------------------------------------------------------------- the local relay
RECORDER = r'''
import os, socket, json, threading
_LOG = os.environ["PC_TEST_NETLOG"]
_lock = threading.Lock()
def _rec(kind, what):
    with _lock:
        with open(_LOG, "a") as f:
            f.write(json.dumps([kind, what]) + "\n")
_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_gai = socket.getaddrinfo
def connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _rec("connect", list(address)[:2])
    return _orig_connect(self, address)
def connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _rec("connect", list(address)[:2])
    return _orig_connect_ex(self, address)
def getaddrinfo(host, *a, **k):
    _rec("resolve", host if isinstance(host, str) else repr(host))
    return _orig_gai(host, *a, **k)
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo
'''


class LocalRelay:
    """The SHIPPED process: `python relay_main.py --os-mode` from `tree` (default: this checkout)."""

    def __init__(self, tmp: str, *, instance_url: str = "", tree=None, record=False, extra_env=None,
                 python=None):
        self.tmp = tmp
        self.tree = Path(tree or os.environ.get("PC_TEST_RELAY_TREE") or ROOT)
        self.port = free_port()
        self.url = "ws://127.0.0.1:%d/" % self.port
        self.dir = os.path.join(tmp, "relay")
        self.ready_file = os.path.join(tmp, "posterchan-relay.url")
        self.desktop_cfg = os.path.join(tmp, "config.json")
        self.netlog = os.path.join(tmp, "net.log")
        self.log = os.path.join(tmp, "relay.log")
        self.python = python or sys.executable
        cfg = {"instance": instance_url}   # "" = relays only; a MISSING key means poster.place (chosen_instance)
        Path(self.desktop_cfg).write_text(json.dumps(cfg))
        env = dict(os.environ)
        env.update({"PC_RELAY_DIR": self.dir, "PC_RELAY_PORT": str(self.port),
                    "PC_DESKTOP_CONFIG": self.desktop_cfg, "PC_RELAY_READY_FILE": self.ready_file,
                    "PC_RELAY_SYNC_SECONDS": "5", "HOME": tmp, "XDG_RUNTIME_DIR": tmp,
                    "PYTHONUNBUFFERED": "1"})
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
            env.pop(k, None)
        if record:
            site = os.path.join(tmp, "site")
            os.makedirs(site, exist_ok=True)
            Path(site, "sitecustomize.py").write_text(RECORDER)
            env["PYTHONPATH"] = site + os.pathsep + env.get("PYTHONPATH", "")
            env["PC_TEST_NETLOG"] = self.netlog
        env.update(extra_env or {})
        self.env = env
        self.proc = None

    def start(self, timeout=60):
        if os.path.exists(self.ready_file):
            os.remove(self.ready_file)
        self._logf = open(self.log, "ab")
        self.proc = subprocess.Popen([self.python, "relay_main.py", "--os-mode"], cwd=str(self.tree),
                                     env=self.env, stdout=self._logf, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError("local relay exited (%s): %s" % (self.proc.returncode, self.output()[-3000:]))
            if os.path.exists(self.ready_file):
                return self
            time.sleep(0.05)
        raise AssertionError("local relay never wrote its ready file: " + self.output()[-3000:])

    def output(self) -> str:
        try:
            return Path(self.log).read_text(errors="replace")
        except OSError:
            return ""

    def io(self) -> dict:
        out = {}
        try:
            for line in Path("/proc/%d/io" % self.proc.pid).read_text().splitlines():
                k, _, v = line.partition(":")
                out[k.strip()] = int(v)
        except (OSError, ValueError):
            pass
        return out

    def rss_kb(self) -> int:
        try:
            for line in Path("/proc/%d/status" % self.proc.pid).read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except OSError:
            pass
        return 0

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        with contextlib.suppress(Exception):
            self._logf.close()

    def connections(self):
        try:
            lines = Path(self.netlog).read_text().splitlines()
        except OSError:
            return []
        return [json.loads(x) for x in lines if x.strip()]


# ------------------------------------------------------------------------------------------- a client
class Client:
    """A minimal NIP-01 client for the local relay: AUTH as the owner, publish, query, and answer the relay's
    PC-SIGN-AUTH the way relay.js does (only for `sign_for`)."""

    def __init__(self, url: str, sk: bytes = OWNER_SK, sign_for: str = ""):
        self.url, self.sk, self.sign_for = url, sk, sign_for
        self.ws = None
        self.challenge = None
        self.subs = {}
        self.oks = {}
        self.signed = 0
        self._task = None

    async def __aenter__(self):
        import websockets
        self.ws = await websockets.connect(self.url, max_size=16 * 1024 * 1024)
        self._task = asyncio.create_task(self._reader())
        for _ in range(100):
            if self.challenge:
                break
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *a):
        self._task.cancel()
        await self.ws.close()

    async def _reader(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m[0] == "AUTH":
                self.challenge = m[1]
            elif m[0] == "OK":
                f = self.oks.pop(m[1], None)
                if f and not f.done():
                    f.set_result((m[2], m[3] if len(m) > 3 else ""))
            elif m[0] in ("EVENT", "EOSE", "CLOSED") and m[1] in self.subs:
                self.subs[m[1]].put_nowait(m)
            elif m[0] == "PC-SIGN-AUTH":
                rid, relay, challenge = m[1], m[2], m[3]
                signed = None
                if self.sign_for and relay == self.sign_for:
                    signed = build_event(self.sk, 22242, "", [["relay", relay], ["challenge", challenge]])
                    self.signed += 1
                await self.ws.send(json.dumps(["PC-SIGNED-AUTH", rid, signed]))

    async def auth(self):
        e = build_event(self.sk, 22242, "", [["relay", self.url], ["challenge", self.challenge]])
        f = asyncio.get_running_loop().create_future()
        self.oks[e["id"]] = f
        await self.ws.send(json.dumps(["AUTH", e]))
        return await asyncio.wait_for(f, 10)

    async def publish(self, e):
        f = asyncio.get_running_loop().create_future()
        self.oks[e["id"]] = f
        await self.ws.send(json.dumps(["EVENT", e]))
        return await asyncio.wait_for(f, 20)

    async def query(self, *filters, timeout=20):
        sid = "q%d" % id(filters)
        q = asyncio.Queue()
        self.subs[sid] = q
        await self.ws.send(json.dumps(["REQ", sid] + list(filters)))
        out = []
        try:
            while True:
                m = await asyncio.wait_for(q.get(), timeout)
                if m[0] == "EVENT":
                    out.append(m[2])
                elif m[0] == "EOSE":
                    return out
                else:
                    raise AssertionError("CLOSED: %s" % (m[2:],))
        finally:
            self.subs.pop(sid, None)
            await self.ws.send(json.dumps(["CLOSE", sid]))


def tmpdir(prefix="pc-osrelay-"):
    return tempfile.mkdtemp(prefix=prefix)
