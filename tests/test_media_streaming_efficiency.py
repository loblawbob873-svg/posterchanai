"""Media Center HLS inside a viewer's byte budget: honest playlists, capped encodes, a low rung.

Measured with scripts/media_hls_bench.py against the real transcoder on nas (viewer cap 1600 kbps):
the playlist advertised 1.2x the nominal rate while real segments peaked at 1.23x (480p) and 1.31x
(360p); a player picked 480p on a lucky estimate and could not keep it (the production 360p<->480p
flip-flop); nothing was offered below ~620 kbps, so a 600 kbps link had no rung that fit; a plain
`-b:v` target spent the ceiling on every picture; and the muxer's PAT/PMT repetition was a tenth of a
240p segment. Each rule below is one of those, and each test fails against the code that had it.
"""
import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import media_center as media
from tests.test_media_center import api, seed  # noqa: F401  (fixture reuse)


def _argv(encoder, profile="360p"):
    return media.command(Path("/media/movie.mkv"), {"video": True, "duration": 60}, profile, 1, encoder,
                         Path("/tmp/out.ts"))


def _value(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


@pytest.mark.parametrize("encoder", ["h264_nvenc", "h264_vaapi", "h264_amf", "libx264"])
def test_the_video_bitrate_is_a_ceiling_for_every_encoder(encoder):
    video = media.PROFILES["360p"][2]
    argv = _argv(encoder)
    assert _value(argv, "-maxrate") == f"{video}k", argv
    assert _value(argv, "-bufsize") == f"{video}k", "a 2x buffer lets a 6s segment run a third over"
    # A quality-targeted mode, never a bare target the encoder pads up to.
    spelling = {"h264_nvenc": ("-cq", "-rc"), "h264_vaapi": ("-rc_mode",), "h264_amf": ("-rc",),
                "libx264": ("-crf",)}[encoder]
    for flag in spelling:
        assert flag in argv, (flag, argv)
    if encoder in ("h264_nvenc", "libx264"):
        assert _value(argv, "-b:v") in (None, "0"), "a non-zero -b:v re-asserts a target"
    if encoder == "h264_vaapi":
        assert _value(argv, "-rc_mode") == "VBR"
    # One IDR per standalone segment: the GOP is at least a whole segment even at 60 fps.
    assert int(_value(argv, "-g")) >= media.SEGMENT * 60
    assert _value(argv, "-profile:v") == "high"


def test_the_muxer_does_not_repeat_tables_inside_a_segment():
    argv = _argv("h264_nvenc")
    assert float(_value(argv, "-pat_period")) >= media.SEGMENT
    assert float(_value(argv, "-sdt_period")) >= media.SEGMENT


def test_a_low_bandwidth_rung_exists_and_fits_a_600_kbps_link():
    offered = media.allowed_profiles(media.DEFAULT_LIMITS)
    assert offered[0] == "240p"
    # hls.js only climbs to (or stays on) a rung whose BANDWIDTH is under 0.7 of its estimate.
    assert media.profile_kbps("240p") <= 600 * 0.7
    # A cap too small for any rung with margin still gets the lowest one, never an empty playlist.
    assert media.allowed_profiles({**media.DEFAULT_LIMITS, "viewer_kbps": 400}) == ["240p"]


def test_only_rungs_a_player_can_sustain_are_offered():
    """The 360p/480p flip-flop: a rung is offered only when its PEAK fits the budget with the margin
    an ABR player needs before it climbs."""
    for cap in (700, 1000, 1600, 3000, 8000):
        config = {**media.DEFAULT_LIMITS, "viewer_kbps": cap}
        for name in media.allowed_profiles(config):
            assert media.profile_kbps(name) * media.ABR_HEADROOM <= cap, (cap, name)
    # The measured line: at the 1600 kbps cap, 480p stalled when advertised at 1116 kbps and played
    # clean at 996 — the highest rung offered must leave the player at least that margin.
    assert media.profile_kbps("480p") * 1600 / 1000 <= 1600
    assert media.allowed_profiles(media.DEFAULT_LIMITS) == ["240p", "360p", "480p"]


def test_the_cache_never_mixes_segments_from_another_encoding(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x")
    monkeypatch.setenv("POSTERCHANAI_MEDIA_ROOTS", str(tmp_path))
    monkeypatch.setenv("POSTERCHANAI_MEDIA_CACHE", "/tmp/pc-test-mc-cache-key")
    stat = source.stat()
    library = {"folder": str(tmp_path), "encoder": "nvidia"}
    item = {"path": "movie.mkv", "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    current = media.segment_cache_location(library, item, "360p", 3)[3]
    monkeypatch.setattr(media, "ENCODING", getattr(media, "ENCODING", 1) + 1, raising=False)
    assert media.segment_cache_location(library, item, "360p", 3)[3] != current
    shutil.rmtree("/tmp/pc-test-mc-cache-key", ignore_errors=True)


def test_an_idle_viewer_gets_a_burst_and_keeps_the_average(monkeypatch):
    """A player with a full buffer stops asking; its next segment used to trickle out at the average
    from the first byte. The bucket banks idle budget, but every byte is still charged."""
    clock = {"now": 1000.0}
    slept = []

    async def sleep(delay):
        slept.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(media, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(media, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(media, "_rate_due", {})
    config = {**media.DEFAULT_LIMITS, "server_kbps": 100000, "viewer_kbps": 1600}
    rate = 1600 * 1000 / 8

    async def drain(size):
        return sum([len(chunk) async for chunk in media.paced_bytes(b"x" * size, "viewer", config)])

    async def exercise():
        monkeypatch.setattr(media, "_rate_lock", asyncio.Lock())
        # A fresh viewer has banked nothing: a segment takes its size / rate.
        start = clock["now"]
        assert await drain(int(rate * 4)) == int(rate * 4)
        assert clock["now"] - start >= 4 - 0.11
        # Idle for ten seconds, then a 3-second segment arrives without waiting.
        clock["now"] += 10
        start = clock["now"]
        assert await drain(int(rate * 3)) == int(rate * 3)
        assert clock["now"] - start < 0.2, "the start of a segment is still starved after idling"
        # ...and a long continuous stream still averages the cap: at most one burst of credit.
        start = clock["now"]
        total = int(rate * 60)
        assert await drain(total) == total
        assert clock["now"] - start >= 60 - media.PACE_BURST_S - 0.2
    asyncio.run(exercise())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_the_playlist_advertises_at_least_the_real_peak(api, tmp_path, monkeypatch):  # noqa: F811
    """BANDWIDTH is the PEAK segment bitrate (RFC 8216). Encode worst-case content — full-frame noise,
    which every encoder spends its whole ceiling on — with the CPU encoder at each offered rung, and
    compare the heaviest real segment against what the master playlist says."""
    client, docs, user, folder = api
    library = seed(docs, folder)
    source = folder / "noise.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "nullsrc=s=854x480:r=24,geq=random(1)*255:128:128",
                    "-f", "lavfi", "-i", "anoisesrc=a=0.5", "-t", "13", "-c:v", "libx264", "-preset", "ultrafast",
                    "-qp", "10", "-c:a", "aac", str(source)], check=True, timeout=120)
    items, _ = media.scan(str(folder))
    item = next(i for i in items if i["path"] == "noise.mkv")
    docs["page:abc:1:0"] = [{**item, "id": "movie"}]
    url = client.post("/api/media-center/abc/play/movie").json()["url"]
    master = client.get(url).text
    advertised = {m.group(2): int(m.group(1)) for m in
                  re.finditer(r"BANDWIDTH=(\d+)[^\n]*\n(\w+)\.m3u8", master)}
    assert set(advertised) == set(media.allowed_profiles(media.DEFAULT_LIMITS))
    for profile, bandwidth in advertised.items():
        for number in (0, 1):
            data = media.transcode({**library, "folder": str(folder)}, item, profile, number)
            real = len(data) * 8 / media.SEGMENT
            assert real <= bandwidth, f"{profile} segment {number}: {real:.0f} bps > advertised {bandwidth}"


# The heaviest real segment of each rung, measured on nas (RTX 3060, NVENC with video_rate_control,
# Bleach TYBW BD anime + a live-action x264 film, 20 segments per rung). Hardware this test cannot run.
MEASURED_NVENC_PEAK_KBPS = {"240p": 399, "360p": 650, "480p": 992}


def test_advertised_peaks_cover_the_measured_hardware_segments():
    for profile, peak in MEASURED_NVENC_PEAK_KBPS.items():
        assert media.profile_kbps(profile) >= peak, (profile, media.profile_kbps(profile), peak)


def test_the_master_playlist_advertises_the_peak_and_codecs(api):  # noqa: F811
    client, docs, user, folder = api
    seed(docs, folder)
    url = client.post("/api/media-center/abc/play/movie").json()["url"]
    master = client.get(url).text
    for profile in media.allowed_profiles(media.DEFAULT_LIMITS):
        assert (f'#EXT-X-STREAM-INF:BANDWIDTH={media.profile_kbps(profile) * 1000},'
                f'CODECS="{media.CODECS[profile]}"\n{profile}.m3u8?') in master, master


def _fake_clock(monkeypatch):
    real = asyncio
    clock = {"now": 5000.0}

    async def sleep(delay):
        clock["now"] += delay
        await real.sleep(0)

    monkeypatch.setattr(media, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(media, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(media, "_rate_due", {})
    monkeypatch.setattr(media, "_streams", {})
    return clock


def test_parallel_segments_of_one_playback_are_delivered_in_order(monkeypatch):
    """The production client fetches four segments at once. Split four ways, the segment the buffer was
    waiting on arrived at a quarter of the budget, got abandoned as too slow, and dragged the other
    three down with it. The budget now goes to the earliest segment first."""
    _fake_clock(monkeypatch)
    config = {**media.DEFAULT_LIMITS, "server_kbps": 100000, "viewer_kbps": 1600}
    arrivals = []

    async def consume(name, number, playback="ticket"):
        async for chunk in media.paced_bytes(b"x" * 16384 * 6, "viewer", config, order=(playback, number)):
            arrivals.append(name)

    async def exercise():
        monkeypatch.setattr(media, "_rate_lock", asyncio.Lock())
        await asyncio.gather(consume("seg7", 7), consume("seg5", 5), consume("seg6", 6))
    asyncio.run(exercise())
    # seg7 got its first chunk before the others existed; from then on the earliest segment drains first.
    assert arrivals == ["seg7"] + ["seg5"] * 6 + ["seg6"] * 6 + ["seg7"] * 5, arrivals
    assert media._streams == {}


def test_a_stalled_earlier_segment_does_not_block_the_rest(monkeypatch):
    clock = _fake_clock(monkeypatch)
    config = {**media.DEFAULT_LIMITS, "server_kbps": 100000, "viewer_kbps": 1600}

    async def exercise():
        monkeypatch.setattr(media, "_rate_lock", asyncio.Lock())
        stuck = media.paced_bytes(b"x" * 16384 * 4, "viewer", config, order=("ticket", 1))
        await anext(stuck)                     # ...and its client never reads again
        started = clock["now"]
        async def drain():
            return [c async for c in media.paced_bytes(b"y" * 16384 * 2, "viewer", config, order=("ticket", 2))]
        later = await asyncio.wait_for(drain(), 10)   # a stuck client must not hold the next segment forever
        assert len(later) == 2 and clock["now"] - started >= media.PRIORITY_IDLE_S
        # A different playback (another tab, another film) is never ordered against this one.
        other = [c async for c in media.paced_bytes(b"z" * 16384, "viewer", config, order=("other", 0))]
        assert len(other) == 1
        await stuck.aclose()
    asyncio.run(exercise())
    assert media._streams == {}


def test_segment_responses_are_ordered_within_their_playback(api, monkeypatch):  # noqa: F811
    client, docs, user, folder = api
    seed(docs, folder)
    calls = []

    async def encoded(*args):
        return b"segment"

    async def spy(data, viewer, config, order=None):
        calls.append(order)
        yield data

    monkeypatch.setattr(media, "segment", encoded)
    monkeypatch.setattr(media, "prefetch", lambda *args: None)
    monkeypatch.setattr(media, "paced_bytes", spy)
    url = client.post("/api/media-center/abc/play/movie").json()["url"]
    assert client.get(url.replace("master.m3u8", "240p-1.ts")).content == b"segment"
    ticket = url.split("ticket=")[1].split("&")[0]
    assert calls == [(ticket, 1)]
