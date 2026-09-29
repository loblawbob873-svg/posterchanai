"""Telegram voice/video calls — app/services/telegram_client/calls.py and the manager's call entry points.

Asked for: "and voice and video call support" for the Telegram client. The call engine (py-tgcalls /
ntgcalls) is replaced by a FAKE with the same method names, so the real hub runs its real state machine;
the frame conversions and the 10 ms audio framing run for real, because those are where a call goes
silent or turns everybody blue without raising anything.
"""
import asyncio
import io

import pytest

from app.services.telegram_client import calls as C


def run(c):
    return asyncio.run(c)


# ---- frame conversion ------------------------------------------------------------------------------

def _jpeg(color, size=(1280, 720)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", quality=95)
    return out.getvalue()


def test_a_camera_frame_becomes_i420_no_bigger_than_the_call_allows():
    frame, w, h = C.i420_from_jpeg(_jpeg((10, 200, 30)))
    assert (w, h) == (640, 360), (w, h)
    assert len(frame) == w * h * 3 // 2, "an I420 frame is exactly width*height*1.5 bytes"
    frame, w, h = C.i420_from_jpeg(_jpeg((10, 200, 30), (333, 201)))
    assert w % 2 == 0 and h % 2 == 0, "odd dimensions cannot be I420"


@pytest.mark.parametrize("rgb", [(220, 20, 20), (20, 220, 20), (20, 20, 220)])
def test_colours_survive_the_round_trip(rgb):
    """A swapped U/V plane turns red into blue and raises nothing — so the colour is measured."""
    from PIL import Image
    frame, w, h = C.i420_from_jpeg(_jpeg(rgb))
    back = Image.open(io.BytesIO(C.jpeg_from_i420(frame, w, h))).convert("RGB")
    assert back.size == (w, h)
    px = back.getpixel((w // 2, h // 2))
    assert all(abs(a - b) < 30 for a, b in zip(px, rgb)), (rgb, px)


def test_a_short_i420_buffer_is_refused_not_read_past():
    with pytest.raises(ValueError):
        C.jpeg_from_i420(b"\x00" * 100, 640, 360)


# ---- audio framing ---------------------------------------------------------------------------------

def test_browser_audio_is_cut_into_exact_10ms_frames():
    f = C.PcmFramer()
    f.push(b"\x01" * 2500)
    assert [len(x) for x in f.frames] == [960, 960], "ntgcalls reads a FULL frame whatever it is handed"
    f.push(b"\x02" * 1340)                    # 580 left over + 1340 = 1920 = two more
    assert len(f.frames) == 4 and all(len(x) == 960 for x in f.frames)


def test_silence_when_the_browser_is_quiet_and_a_bounded_backlog():
    f = C.PcmFramer(backlog=5)
    assert f.pop() == bytes(960), "no audio yet must still be a full frame of silence"
    f.push(bytes(range(256)) * 60)            # 15360 bytes = 16 frames
    assert len(f.frames) == 5 and f.dropped == 11, "a stalled engine must drop old audio, not queue it"


# ---- the call state machine --------------------------------------------------------------------------

pytgcalls = pytest.importorskip("pytgcalls")
from pytgcalls.types import Device  # noqa: E402


class CallDeclined(Exception):
    pass


class CallBusy(Exception):
    pass


class FakeEngine:
    """py-tgcalls' PyTgCalls, as far as the hub uses it."""

    def __init__(self, client):
        self.client, self.handlers, self.played, self.recorded, self.left, self.sent = client, [], [], [], [], []
        self.fail = None
        self.started = False
        self.answer = asyncio.Event()
        self.auto_answer = True

    def on_update(self, filters=None):
        def deco(fn):
            self.handlers.append((filters, fn))
            return fn
        return deco

    async def start(self):
        self.started = True

    async def play(self, peer, stream, config=None):
        self.played.append((peer, bool(stream.camera) if hasattr(stream, "camera") else None))
        if self.fail:
            raise self.fail
        if not self.auto_answer:
            await self.answer.wait()

    async def record(self, peer, stream, config=None):
        self.recorded.append(peer)

    async def leave_call(self, peer, close=False):
        self.left.append(peer)

    async def send_frame(self, peer, device, data, info=None):
        self.sent.append((peer, device, len(data), info))


class Client:
    def add_event_handler(self, fn, ev):
        pass

    async def get_entity(self, peer):
        class U:
            first_name, last_name, username, id = "Alice", "", "", peer
        return U()


def _hub():
    events, made = [], []

    async def emit(ev):
        events.append(ev)

    def factory(client):
        e = FakeEngine(client)
        made.append(e)
        return e
    return C.CallHub(Client(), emit, tgcalls_factory=factory), events, made


def _settle():
    return asyncio.sleep(0.05)


def test_an_outgoing_call_rings_then_connects_and_records_the_other_side():
    async def go():
        hub, events, made = _hub()
        r = await hub.start(42, video=False)
        assert r["state"] == "calling"
        await _settle()
        eng = made[0]
        assert eng.played[0][0] == 42 and eng.recorded == [42], (eng.played, eng.recorded)
        assert hub.state == "active"
        states = [e["state"] for e in events]
        assert states[:2] == ["calling", "active"] and events[0]["title"] == "Alice", events
        with pytest.raises(C.CallError):
            await hub.start(43, video=False)          # one call at a time
        await hub.hangup()
    run(go())


@pytest.mark.parametrize("exc,reason", [(CallDeclined(), "declined"), (CallBusy(), "busy")])
def test_a_refused_call_ends_and_says_why(exc, reason):
    async def go():
        hub, events, made = _hub()
        await hub._engine()
        made[0].fail = exc
        await hub.start(42, video=True)
        await _settle()
        assert hub.state == "idle"
        assert events[-1]["state"] == "idle" and events[-1]["reason"] == reason, events
    run(go())


def test_an_incoming_call_rings_and_can_be_answered_or_declined():
    async def go():
        hub, events, made = _hub()
        await hub.ringing(42)
        assert hub.state == "ringing" and events[-1]["state"] == "ringing" and events[-1]["title"] == "Alice"
        # A second caller while this one rings hears busy and changes nothing here.
        await hub.ringing(77)
        assert made[0].left == [77] and hub.peer == 42
        await hub.accept(42, video=True)
        assert hub.state == "active" and hub.video and made[0].recorded == [42]
        await hub.hangup()
        assert made[0].left[-1] == 42 and events[-1]["state"] == "idle"
        await hub.ringing(55)
        await hub.hangup()
        assert events[-1]["reason"] == "declined", "hanging up a RINGING call is declining it"
    run(go())


@pytest.mark.parametrize("busy", [False, True])
def test_the_other_side_hanging_up_ends_the_call_on_screen(busy):
    """Fires Telegram's own DISCARDED_CALL update through the handler the hub registers."""
    from pytgcalls.types import ChatUpdate

    async def go():
        hub, events, made = _hub()
        await hub.ringing(42)
        await hub.accept(42, video=False)
        status = ChatUpdate.Status.DISCARDED_CALL | (ChatUpdate.Status.BUSY_CALL if busy else ChatUpdate.Status(0))
        for flt, fn in made[0].handlers:
            if flt is not None and "DISCARDED" in repr(getattr(flt, "_flags", flt)) or fn.__name__ == "_gone":
                await fn(None, ChatUpdate(42, status))
        assert hub.state == "idle", "the other side hung up and the call stayed open"
        assert events[-1]["state"] == "idle" and events[-1]["reason"] == ("busy" if busy else "hung up"), events[-1]
    run(go())


def test_the_camera_toggles_on_the_live_call():
    async def go():
        hub, events, made = _hub()
        await hub.ringing(42)
        await hub.accept(42, video=False)
        await hub.camera(True)
        assert hub.video and events[-1]["video"] is True
        assert len(made[0].played) == 2, "the camera must be swapped in on the SAME call, not redialled"
        await hub.hangup()
        with pytest.raises(C.CallError):
            await hub.camera(True)
    run(go())


def test_the_pacer_sends_one_full_10ms_frame_every_10ms():
    async def go():
        hub, events, made = _hub()
        await hub.ringing(42)
        await hub.accept(42, video=False)
        await hub.from_browser(bytes([C.PCM]) + b"\x05" * 960 * 3)
        await asyncio.sleep(0.2)
        await hub.hangup()
        mic = [s for s in made[0].sent if s[1] == Device.MICROPHONE]
        assert 12 <= len(mic) <= 26, f"{len(mic)} audio frames in 200 ms — the call audio would stutter or rush"
        assert all(s[2] == 960 for s in mic), "every frame handed to the engine is exactly 10 ms"
    run(go())


def test_the_browser_camera_reaches_the_call_only_while_it_is_on():
    async def go():
        hub, events, made = _hub()
        await hub.ringing(42)
        await hub.accept(42, video=False)
        await hub.from_browser(bytes([C.JPEG]) + _jpeg((1, 2, 3)))
        assert not [s for s in made[0].sent if s[1] == Device.CAMERA], "a camera frame was sent with the camera off"
        await hub.camera(True)
        await hub.from_browser(bytes([C.JPEG]) + _jpeg((1, 2, 3)))
        cam = [s for s in made[0].sent if s[1] == Device.CAMERA]
        assert cam and cam[0][2] == 640 * 360 * 3 // 2 and cam[0][3].width == 640
        await hub.hangup()
    run(go())


class Sock:
    def __init__(self):
        self.got = []

    async def send_bytes(self, b):
        self.got.append(bytes(b))


def test_the_other_side_reaches_the_browser_as_pcm_and_jpeg():
    from pytgcalls.types import Device, Frame

    class F:
        def __init__(self, data, w=0, h=0):
            self.frame, self.info = data, Frame.Info(width=w, height=h)

    class U:
        def __init__(self, device, frames):
            self.device, self.frames, self.chat_id = device, frames, 42

    async def go():
        hub, events, made = _hub()
        await hub.ringing(42)
        await hub.accept(42, video=False)
        s = Sock()
        hub.sockets.add(s)
        await hub.remote_frames(U(Device.MICROPHONE, [F(b"\x01" * 960), F(b"\x02" * 960)]))
        assert s.got[0][0] == C.PCM_OUT and len(s.got[0]) == 1 + 1920
        i420, w, h = C.i420_from_jpeg(_jpeg((200, 30, 30)))
        await hub.remote_frames(U(Device.CAMERA, [F(i420, w, h)]))
        jp = [g for g in s.got if g[0] == C.JPEG_OUT]
        assert jp and jp[0][1:3] == b"\xff\xd8", "remote video must arrive as a JPEG"
        assert events[-1]["remote_video"] is True
        await hub.remote_frames(U(Device.CAMERA, [F(i420, w, h)]))
        assert len([g for g in s.got if g[0] == C.JPEG_OUT]) == 1, "remote video was not throttled"
        await hub.hangup()
    run(go())


def test_both_requirement_lists_install_the_call_engine():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for req in ("requirements.txt", "requirements-nostr.txt"):
        assert "py-tgcalls" in (root / req).read_text(), f"{req} does not install the call engine"
