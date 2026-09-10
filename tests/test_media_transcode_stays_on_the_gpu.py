"""A TIMEOUT IS NOT A BROKEN ENCODER, AND THE TRANSCODER MUST SAY WHAT IT DID.

Reported as "why did media player stutter just now" and "i need media player stable", streaming to a
TV through poster.place from the nas GPU.

MEASURED from the outside, because the component itself said nothing: three PlaySessionIds inside
one hour, and both items served far more 360p segments than 480p (169 vs 46, 112 vs 18) — the player
stepping down and then restarting, which is what late segments look like from a sofa.

THE CHAIN. A segment is `SEGMENT` (6) seconds of video and a hardware encoder gets a 10s budget. On
ANY exception — including a plain `TimeoutExpired` — the encoder was put in `_failed_encoders` for
300 seconds, so EVERY following segment for five minutes was transcoded by libx264 on the CPU. This
box shares its GPU with music and video generation, so one busy moment, one cold seek or one leaked
NVENC session (nvidia-smi was holding 125 MiB for an ffmpeg PID that no longer existed) demoted the
whole stream. It is a latch set on a transient, the shape this repo keeps rediscovering.

And it was invisible: ffmpeg ran with stdout AND stderr to DEVNULL, so the one component that knows
which encoder ran, how long the segment took and why the GPU was abandoned wrote nothing anywhere.
"""
import inspect
import re
import subprocess

from app.services import media_center as mc


def _fn():
    return inspect.getsource(mc.segment) if hasattr(mc, "segment") else inspect.getsource(mc)


SRC = inspect.getsource(mc)


def test_a_timeout_has_its_own_branch():
    assert "except subprocess.TimeoutExpired:" in SRC, \
        "a timeout is still handled by the same except that bans the encoder"


def test_a_timeout_does_not_blacklist_the_encoder():
    """The whole bug. One slow segment must cost that segment, not five minutes of GPU."""
    block = SRC[SRC.index("except subprocess.TimeoutExpired:"):]
    block = block[:block.index("except (OSError")]
    assert "_failed_encoders" not in block, block


def test_a_real_refusal_still_is_remembered():
    """A missing encoder or a dead device is worth not retrying 200 times a minute."""
    block = SRC[SRC.index("except (OSError, subprocess.SubprocessError)"):]
    assert "_failed_encoders[encoder] = time.monotonic() + 300" in block


def test_ffmpeg_stderr_is_kept():
    assert "stderr=subprocess.PIPE" in SRC, "ffmpeg's reason is still discarded"
    assert "stderr=subprocess.DEVNULL" not in SRC


def test_every_failure_path_says_something():
    for needle in ("timed out on segment", "refused segment", "libx264 failed on segment"):
        assert needle in SRC, needle


def test_a_slow_segment_is_reported_even_when_it_succeeds():
    """The viewer feels this before any error exists to find: a 6s segment that took 9s arrived
    late, and nothing was wrong enough to raise."""
    assert "took > SEGMENT" in SRC
    assert "for %ss of video" in SRC


def test_the_logger_exists_at_import_time():
    """Every one of these calls is inside an `except`. This repo has already taken an outage from
    an except that called an undefined `logger`: the guard raised NameError and every request 502'd."""
    assert isinstance(mc.logger.name, str) and mc.logger.name.endswith("media_center")


def test_the_hardware_budget_is_still_bounded():
    """Removing the ban must not turn into waiting forever on a wedged GPU."""
    assert re.search(r"timeout=45 if encoder == \"libx264\" else 10", SRC)
