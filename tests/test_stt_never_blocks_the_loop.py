"""Speech-to-text never freezes the server: it runs off the event loop, one at a time, with a time limit.

2026-10-15 13:19-13:46: poster.place stopped answering for 27 minutes. The last line the app logged was
"[faster_whisper] Processing audio with duration 00:04.619": transcribe_audio was async but ran model.transcribe (and
walked its lazy segments) ON the event loop of the single uvicorn worker, and on a CPU saturated by a test gate that
clip never finished. Driven here with fake models: one that takes a second, one that never finishes.
"""
import asyncio
import threading
import time

from app.services import stt_service


class _Seg:
    def __init__(self, t):
        self.text = t


class _Info:
    language = "en"
    duration = 1.0


class _Slow:
    def __init__(self, seconds):
        self.seconds = seconds

    def transcribe(self, path, **kw):
        def segs():
            time.sleep(self.seconds)          # the decode happens while the segments are walked
            yield _Seg("hello")
        return segs(), _Info()


def _drive(model, timeout=None, monkeypatch=None):
    monkeypatch.setattr(stt_service, "get_model", lambda: model)
    monkeypatch.setattr(stt_service, "_stt_gate", None)
    if timeout is not None:
        monkeypatch.setattr(stt_service, "_STT_TIMEOUT_S", timeout)

    async def go():
        ticks = []

        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)
        t = asyncio.create_task(ticker())
        t0 = time.monotonic()
        result = await stt_service.transcribe_audio(b"RIFF....", "en")
        took = time.monotonic() - t0
        t.cancel()
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        return result, took, max(gaps or [0])
    return asyncio.run(go())


def test_a_transcription_does_not_stop_the_server_answering(monkeypatch):
    result, took, worst_gap = _drive(_Slow(1.0), monkeypatch=monkeypatch)
    assert result[0] == "hello", result
    assert took >= 0.9
    assert worst_gap < 0.5, ("the event loop froze for %.2fs while transcribing" % worst_gap)


def test_a_transcription_that_never_ends_is_given_up_and_the_loop_kept_running(monkeypatch):
    stop = threading.Event()

    class _Stuck:
        def transcribe(self, path, **kw):
            def segs():
                stop.wait(10)
                yield _Seg("late")
            return segs(), _Info()
    try:
        result, took, worst_gap = _drive(_Stuck(), timeout=0.5, monkeypatch=monkeypatch)
    finally:
        stop.set()
    assert result == (None, None), result
    assert took < 3, ("the time limit did not apply: %.1fs" % took)
    assert worst_gap < 0.5, ("the event loop froze for %.2fs" % worst_gap)
