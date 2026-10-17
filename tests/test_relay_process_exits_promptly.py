"""The relay process exits as soon as its teardown is done -- it is not SIGKILLed at systemd's stop timeout.

server1, 2026-10-17: every relay stop logged "[nostr-relay] stopped" and then hung until systemd's
TimeoutStopSec=10 killed it ('stop-sigterm' timed out, SIGKILL), because a normal interpreter exit joins every
non-daemon thread still running (an executor worker mid-query, an outbound socket waiting out a timeout). Each
deploy's relay restart took ~12 s. Runs the SHIPPED relay_main.main() with a stand-in relay and a thread that
never ends.
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
import sys, threading, time, types, asyncio
sys.path.insert(0, %r)
import relay_main
stub = types.SimpleNamespace()
stub._relay = types.SimpleNamespace(stop_event=None)
stub.open_store_for_settings = lambda loop: object()
stub._read_config = lambda: {"enabled": True}
async def _main(cfg, store):
    print("stopped", flush=True)          # the real teardown's last line; the store is closed by now
stub._main = _main
relay_main._t = stub
threading.Thread(target=lambda: time.sleep(60), daemon=False).start()   # a worker that will not finish
relay_main.main()
print("returned", flush=True)
""" % str(ROOT)


def test_the_relay_process_exits_promptly_even_with_a_stuck_worker_thread():
    t = time.monotonic()
    p = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=30)
    took = time.monotonic() - t
    assert "stopped" in p.stdout, p.stderr
    assert took < 5, ("the relay process waited %.1f s for a stray thread after stopping" % took)
    assert p.returncode == 0, (p.returncode, p.stderr[-500:])


def test_the_mirror_and_the_close_handshakes_are_waited_for_side_by_side():
    src = (ROOT / "app/services/nostr_relay/thread.py").read_text()
    stop = src[src.index("outbox.stop()\n        if private is not None:"):src.index("await store.checkpoint()")]
    assert "asyncio.gather(" in stop and "server.mirror.stop()" in stop and "ws.wait_closed()" in stop, stop
    assert "wait_for(server.mirror.stop(), 5)" not in src, "the mirror's 5 s stop is back in series before the close"
