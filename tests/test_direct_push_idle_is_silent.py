"""An idle PosterChan Direct socket must not wake the phone, and must still deliver.

"bad battery drain on my phone… 10 percent used for today so far": the notification socket lives in a
foreground service and sits idle in a pocket all day, and it carried three unaligned keepalives — an
app ping from the server after 20s of quiet, uvicorn's protocol ping every 20s and the APK's OkHttp
ping every 30s. Each frame wakes the radio. Now: no app ping at all, and one protocol ping each way
every 75s (inside Cloudflare's 100s idle window).

The serve loop's clock is compressed (every asyncio.wait timeout ÷400) so a real idle minute runs in
a fraction of a second, against the SHIPPED loop.
"""
import asyncio
import re
from pathlib import Path

import pytest

from app.services import direct_push_service as direct
from tests import test_direct_push_server as server_tests
from tests.test_direct_push_server import _direct_row

ROOT = Path(__file__).resolve().parents[1]
SCALE = 400


@pytest.fixture()
def direct_db(monkeypatch):
    return server_tests.direct_db.__wrapped__(monkeypatch)


class _FastAsyncio:
    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def wait(fs, timeout=None, **kw):
        return await asyncio.wait(fs, timeout=None if timeout is None else timeout / SCALE, **kw)


def _run_idle(monkeypatch, sid, seconds, before=None):
    monkeypatch.setattr(direct, "asyncio", _FastAsyncio())
    sent = []

    async def exercise():
        class Socket:
            async def send_json(self, frame):
                sent.append(frame)

            async def receive_json(self):
                await asyncio.Future()
        task = asyncio.create_task(direct.serve(Socket(), sid))
        if before:
            await asyncio.sleep(.02)
            before()
        await asyncio.sleep(seconds / SCALE)
        task.cancel()
        await task
    asyncio.run(exercise())
    return sent


def test_an_idle_socket_sends_the_phone_nothing_for_ten_minutes(direct_db, monkeypatch):
    db = direct_db()
    sid = _direct_row(db).id
    sent = _run_idle(monkeypatch, sid, 600)
    assert sent == [], f"{len(sent)} frames in ten idle minutes: {sent[:3]}"


def test_a_notification_queued_by_another_process_still_arrives_within_the_poll(direct_db, monkeypatch):
    """The worker process cannot set this process's wake event; the poll is what delivers it."""
    db = direct_db()
    sid = _direct_row(db).id
    monkeypatch.setattr(direct, "wake", lambda *_: None)      # exactly the cross-process case
    sent = _run_idle(monkeypatch, sid, 45, before=lambda: direct.enqueue_result(sid, {"type": "test"}))
    # (Re-sent each poll until the phone ACKs it, which this fake never does — that is the queue's
    # durability, not a keepalive.)
    assert sent and {f["type"] for f in sent} == {"notification"}, sent


def test_every_keepalive_is_inside_cloudflares_window_and_no_tighter_than_a_minute():
    run = (ROOT / "run.py").read_text()
    m = re.search(r"ws_ping_interval=(\d+)", run)
    assert m and 60 <= int(m.group(1)) < 100, "uvicorn pings every socket every 20s by default"
    java = (ROOT / "mobile/android/app/src/main/java/place/poster/app/push/DirectPushService.java").read_text()
    m = re.search(r"PING_S = (\d+)L", java)
    assert m and 60 <= int(m.group(1)) < 100 and ".pingInterval(PING_S," in java
