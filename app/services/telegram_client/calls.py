"""Telegram voice and video calls (1:1), bridged between Telegram and the browser.

Telegram's call media is its own VoIP protocol, which no browser speaks and Telethon does not carry.
`py-tgcalls` + `ntgcalls` (LGPLv3) run it here, on the node, against the account's Telethon client,
and this module is the BRIDGE: the browser sends its microphone as PCM and its camera as JPEG frames
over one binary websocket (`/api/tgc/call-media`), and receives the other side's audio and video the
same way. Nothing is decoded in the browser that a browser cannot already decode.

Frame formats, fixed by ntgcalls (read in its source, not assumed):
  * audio   — PCM16 little-endian, 48 kHz MONO, EXACTLY 10 ms per send_frame (960 bytes). The engine
              ignores the length it is handed and always reads a full frame, so a short buffer reads
              past the end of ours: every frame is padded or split to exactly FRAME_BYTES.
  * video   — raw I420 (Y, then U, then V), even width and height.

Wire protocol (first byte = type):
  browser → node   0x01 PCM (any whole number of samples)     0x02 JPEG (one camera frame)
  node → browser   0x11 PCM (whole 10 ms frames)              0x12 JPEG (one remote camera frame)
Control (start/accept/hang up/camera) is HTTP, and call state rides the account's existing event
socket as `{"type": "call", ...}`, so a ringing call reaches every window even with no media socket.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time

logger = logging.getLogger(__name__)

RATE = 48000
FRAME_BYTES = RATE // 100 * 2          # 10 ms of 48 kHz mono PCM16
MAX_BACKLOG = 30                       # 300 ms: past this the oldest audio is dropped, not delayed
VIDEO_MAX_W, VIDEO_MAX_H = 640, 360
VIDEO_FPS_OUT = 12                     # remote video sent to the browser
PCM, JPEG, PCM_OUT, JPEG_OUT = 0x01, 0x02, 0x11, 0x12


def available() -> bool:
    import importlib.util
    try:
        return all(importlib.util.find_spec(m) is not None for m in ("pytgcalls", "ntgcalls"))
    except Exception:
        return False


# ---- frame conversion -----------------------------------------------------------------------------

def _even(n: int) -> int:
    return max(2, int(n) - int(n) % 2)


def i420_from_jpeg(data: bytes, max_w: int = VIDEO_MAX_W, max_h: int = VIDEO_MAX_H):
    """A browser's camera frame (JPEG) → (I420 bytes, width, height), never larger than max_w×max_h."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (max_w, max_h))
    im = im.convert("RGB")
    scale = min(1.0, max_w / im.width, max_h / im.height)
    w, h = _even(im.width * scale), _even(im.height * scale)
    if (w, h) != im.size:
        im = im.resize((w, h), Image.BILINEAR)
    y, cb, cr = im.convert("YCbCr").split()
    half = (w // 2, h // 2)
    return y.tobytes() + cb.resize(half, Image.BOX).tobytes() + cr.resize(half, Image.BOX).tobytes(), w, h


def jpeg_from_i420(data: bytes, w: int, h: int, quality: int = 60,
                   max_w: int = VIDEO_MAX_W, max_h: int = VIDEO_MAX_H) -> bytes:
    """The other side's camera frame (I420) → a JPEG the browser can draw."""
    import numpy as np
    from PIL import Image
    w, h = int(w), int(h)
    if w < 2 or h < 2 or len(data) < w * h * 3 // 2:
        raise ValueError("not an I420 frame of that size")
    buf = np.frombuffer(data, dtype=np.uint8, count=w * h * 3 // 2)
    cw, ch = (w + 1) // 2, (h + 1) // 2
    y = buf[:w * h].reshape(h, w)
    u = buf[w * h:w * h + cw * ch].reshape(ch, cw).repeat(2, 0).repeat(2, 1)[:h, :w]
    v = buf[w * h + cw * ch:w * h + 2 * cw * ch].reshape(ch, cw).repeat(2, 0).repeat(2, 1)[:h, :w]
    im = Image.fromarray(np.dstack((y, u, v)), "YCbCr").convert("RGB")
    scale = min(1.0, max_w / w, max_h / h)
    if scale < 1.0:
        im = im.resize((_even(w * scale), _even(h * scale)), Image.BILINEAR)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality)
    return out.getvalue()


class PcmFramer:
    """Browser audio arrives in whatever sizes the browser sent; Telegram takes exactly 10 ms at a
    time. Splits into FRAME_BYTES frames, keeps an odd tail for the next push, and bounds the backlog
    so a stalled engine costs a skip in the audio, never a growing delay."""

    def __init__(self, backlog: int = MAX_BACKLOG):
        self.frames: list = []
        self._tail = b""
        self.backlog = backlog
        self.dropped = 0

    def push(self, data: bytes) -> None:
        buf = self._tail + bytes(data)
        whole = len(buf) - len(buf) % FRAME_BYTES
        for i in range(0, whole, FRAME_BYTES):
            self.frames.append(buf[i:i + FRAME_BYTES])
        self._tail = buf[whole:]
        if len(self.frames) > self.backlog:
            over = len(self.frames) - self.backlog
            del self.frames[:over]
            self.dropped += over

    def pop(self) -> bytes:
        """The next 10 ms — silence when the browser has sent nothing, since ntgcalls expects a
        steady stream and a gap would read as the call dropping."""
        return self.frames.pop(0) if self.frames else bytes(FRAME_BYTES)


# ---- one account's calls ----------------------------------------------------------------------------

class CallHub:
    """Every call of ONE signed-in account: at most one at a time, like the phone apps."""

    def __init__(self, client, emit, tgcalls_factory=None, clock=time.monotonic):
        self.client, self.emit, self._factory, self._clock = client, emit, tgcalls_factory, clock
        self.tg = None
        self.peer = 0
        self.state = "idle"            # idle | ringing | calling | active
        self.video = False              # the camera we send
        self.remote_video = False
        self.title = ""
        self.framer = PcmFramer()
        self.sockets: set = set()
        self._pacer = None
        self._last_video_out = 0.0
        self._video_busy = False
        self._started = False

    # -- engine ----------------------------------------------------------------------------------
    async def _engine(self):
        if self.tg is not None and self._started:
            return self.tg
        if self._factory is not None:
            self.tg = self._factory(self.client)
        else:
            from pytgcalls import PyTgCalls
            from pytgcalls.pytgcalls_session import PyTgCallsSession
            PyTgCallsSession.notice_displayed = True      # no banner on stdout, no GitHub update check
            self.tg = PyTgCalls(self.client, workers=2)
            # ntgcalls-3.0.0 / py-tgcalls-3.0.0: a CONNECTED private call that drops calls
            # discard_call(chat_id) without its required `is_missed`, raising inside a future nobody
            # reads — the call is never ended on Telegram's side. Default the argument.
            app = getattr(self.tg, "_app", None)
            orig = getattr(app, "discard_call", None)
            if orig is not None:
                async def discard_call(chat_id, is_missed=False, _orig=orig):
                    return await _orig(chat_id, is_missed)
                app.discard_call = discard_call
        self._wire(self.tg)
        await self.tg.start()
        self._started = True
        return self.tg

    def _wire(self, tg):
        from pytgcalls import filters as fl
        from pytgcalls.types import ChatUpdate, Direction, UpdatedEmojis

        @tg.on_update(fl.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def _ring(_, u):
            await self.ringing(int(u.chat_id))

        @tg.on_update(fl.chat_update(ChatUpdate.Status.DISCARDED_CALL))
        async def _gone(_, u):
            if int(u.chat_id) == self.peer:
                status = getattr(u, "status", None)       # a Flag, not an int: int() of it raises
                busy = bool(status is not None and status & ChatUpdate.Status.BUSY_CALL)
                await self.ended("busy" if busy else "hung up")

        @tg.on_update(fl.stream_frame(directions=Direction.INCOMING))
        async def _frames(_, u):
            if int(u.chat_id) == self.peer:
                await self.remote_frames(u)

        @tg.on_update()
        async def _emojis(_, u):
            if isinstance(u, UpdatedEmojis) and int(getattr(u, "chat_id", 0) or 0) == self.peer:
                await self._say(emojis=str(getattr(u, "emojis", "") or ""))

    def watch_requests(self, client) -> None:
        """Telegram marks a VIDEO call on the request itself; py-tgcalls does not pass that on, so the
        account's own Telethon client reads it (UpdatePhoneCall → PhoneCallRequested.video)."""
        async def on_raw(update):
            call = getattr(update, "phone_call", None)
            if type(update).__name__ != "UpdatePhoneCall" or type(call).__name__ != "PhoneCallRequested":
                return
            self.remote_video = bool(getattr(call, "video", False))
            if self.state == "ringing" and self.peer == int(getattr(call, "admin_id", 0) or 0):
                await self._say()
        try:
            from telethon import events
            client.add_event_handler(on_raw, events.Raw())
        except Exception:
            pass

    # -- state ------------------------------------------------------------------------------------
    async def _say(self, **extra):
        ev = {"type": "call", "peer": self.peer, "title": self.title, "state": self.state,
              "video": self.video, "remote_video": self.remote_video}
        ev.update(extra)
        await self.emit(ev)

    async def _title_of(self, peer: int) -> str:
        try:
            from app.services.telegram_client.manager import _name
            return _name(await self.client.get_entity(int(peer)))
        except Exception:
            return str(peer)

    async def ringing(self, peer: int):
        if self.state != "idle" and self.peer != peer:
            try:                                            # already on a call: they hear busy
                await (await self._engine()).leave_call(peer)
            except Exception:
                pass
            return
        self.peer, self.state, self.video = int(peer), "ringing", False
        self.title = await self._title_of(peer)
        await self._say()

    def _stream(self, video: bool):
        from pytgcalls.types import ExternalMedia, MediaStream
        from pytgcalls.types.raw import AudioParameters, VideoParameters
        media = ExternalMedia.AUDIO | ExternalMedia.VIDEO if video else ExternalMedia.AUDIO
        return MediaStream(media, AudioParameters(RATE, 1), VideoParameters(VIDEO_MAX_W, VIDEO_MAX_H, 15))

    async def _connect(self, video: bool, timeout: int = 45):
        from pytgcalls.types import CallConfig, RecordStream
        from pytgcalls.types.raw import AudioParameters
        tg = await self._engine()
        await tg.play(self.peer, self._stream(video), CallConfig(timeout=timeout))
        await tg.record(self.peer, RecordStream(audio=True, audio_parameters=AudioParameters(RATE, 1), camera=True))
        self.state = "active"
        self._start_pacer()
        await self._say()

    async def start(self, peer: int, video: bool) -> dict:
        if self.state != "idle":
            raise CallError("You are already on a call.")
        self.peer, self.state, self.video, self.remote_video = int(peer), "calling", bool(video), False
        self.title = await self._title_of(peer)
        await self._say()
        asyncio.get_running_loop().create_task(self._run_outgoing(video))
        return {"peer": self.peer, "state": self.state}

    async def _run_outgoing(self, video: bool):
        try:
            await self._connect(video)
        except Exception as e:
            await self.ended(_reason(e))

    async def accept(self, peer: int, video: bool) -> dict:
        if self.state != "ringing" or self.peer != int(peer):
            raise CallError("That call is no longer ringing.")
        self.video = bool(video)
        self.state = "calling"
        await self._say()
        try:
            await self._connect(self.video)
        except Exception as e:
            await self.ended(_reason(e))
            raise CallError("The call could not be connected: " + _reason(e))
        return {"peer": self.peer, "state": self.state}

    async def hangup(self, peer: int | None = None) -> dict:
        if self.state == "idle":
            return {"state": "idle"}
        p = self.peer
        try:
            await (await self._engine()).leave_call(p)
        except Exception:
            pass
        await self.ended("declined" if self.state == "ringing" else "hung up")
        return {"state": "idle"}

    async def camera(self, on: bool) -> dict:
        """Turn OUR camera on or off mid-call: a new stream on the live call (py-tgcalls swaps the
        sources rather than redialling)."""
        if self.state != "active":
            raise CallError("There is no call to change.")
        await (await self._engine()).play(self.peer, self._stream(bool(on)))
        self.video = bool(on)
        await self._say()
        return {"video": self.video}

    async def ended(self, reason: str):
        if self.state == "idle":
            return
        self.state = "idle"
        if self._pacer:
            self._pacer.cancel()
            self._pacer = None
        self.framer = PcmFramer()
        await self._say(reason=reason)
        self.peer, self.video, self.remote_video, self.title = 0, False, False, ""

    # -- media ------------------------------------------------------------------------------------
    def _start_pacer(self):
        if self._pacer:
            self._pacer.cancel()
        self._pacer = asyncio.get_running_loop().create_task(self._pace())

    async def _pace(self):
        """One 10 ms frame every 10 ms, on a clock rather than a sleep-per-frame, so a slow send
        never turns into drift the other side hears as a slowly growing delay."""
        from pytgcalls.types import Device
        t0, n = self._clock(), 0
        while self.state == "active":
            try:
                await self.tg.send_frame(self.peer, Device.MICROPHONE, self.framer.pop())
            except Exception as e:
                logger.debug("tg call audio frame refused: %s", e)
            n += 1
            wait = t0 + n * 0.010 - self._clock()
            if wait < -0.2:                                 # fell far behind: start the clock again
                t0, n = self._clock(), 0
                wait = 0
            await asyncio.sleep(max(0.0, wait))

    async def from_browser(self, data: bytes):
        if not data or self.state != "active":
            return
        kind, body = data[0], bytes(data[1:])
        if kind == PCM:
            self.framer.push(body)
        elif kind == JPEG and self.video and len(body) <= 512 * 1024:
            from pytgcalls.types import Device, Frame
            try:
                frame, w, h = await asyncio.to_thread(i420_from_jpeg, body)
                await self.tg.send_frame(self.peer, Device.CAMERA, frame, Frame.Info(width=w, height=h))
            except Exception as e:
                logger.debug("tg call video frame refused: %s", e)

    async def remote_frames(self, u):
        from pytgcalls.types import Device
        dev = getattr(u, "device", None)
        frames = list(getattr(u, "frames", None) or [])
        if not frames or not self.sockets:
            return
        if dev == Device.MICROPHONE:
            pcm = b"".join(bytes(f.frame) for f in frames)
            await self._send(bytes([PCM_OUT]) + pcm)
        elif dev == Device.CAMERA:
            now = self._clock()
            if self._video_busy or now - self._last_video_out < 1.0 / VIDEO_FPS_OUT:
                return                                      # the browser gets ≤12 fps; the rest is dropped
            f = frames[-1]
            self._video_busy, self._last_video_out = True, now
            try:
                jpg = await asyncio.to_thread(jpeg_from_i420, bytes(f.frame), f.info.width, f.info.height)
                if not self.remote_video:
                    self.remote_video = True
                    await self._say()
                await self._send(bytes([JPEG_OUT]) + jpg)
            except Exception as e:
                logger.debug("tg call remote video frame dropped: %s", e)
            finally:
                self._video_busy = False

    async def _send(self, payload: bytes):
        for ws in list(self.sockets):
            try:
                await ws.send_bytes(payload)
            except Exception:
                self.sockets.discard(ws)


class CallError(Exception):
    pass


def _reason(e: Exception) -> str:
    n = type(e).__name__
    return {"CallDeclined": "declined", "CallBusy": "busy", "TimedOutAnswer": "no answer",
            "CallDiscarded": "hung up", "TelegramServerError": "could not reach Telegram's call servers",
            "UnMuteNeeded": "muted"}.get(n, (str(e) or n)[:200])
