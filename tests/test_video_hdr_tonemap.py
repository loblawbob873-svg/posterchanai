"""A phone's HDR video must come back as something a browser can actually decode.

Run: venv-unified/bin/python -m pytest tests/test_video_hdr_tonemap.py

Every recent iPhone and Android records 10-bit HLG/PQ by default. ffmpeg follows its input, so
handing one to libx264 with no instructions produced H.264 **High 10** carrying the source's bt2020
tags — and both halves of that are what people reported:

  * High 10 AVC is not decodable by Chrome, Safari, Android MediaCodec or iOS. The upload succeeds,
    the post looks normal, and the video **will not play** for anybody.
  * Where something does decode it, BT.2020/HLG rendered as BT.709 crushes blacks and blows
    highlights — reported, in those words, as **"super high contrast"**.

Measured before the fix, through the real `compress_video_file`:

    profile=High 10  pix_fmt=yuv420p10le  color_space=bt2020nc

i.e. unchanged in every way that mattered, and smaller in the one that did not.

THESE TESTS RUN FFMPEG. A string assertion on the command is exactly what would have passed while
this was broken — the old command was perfectly well-formed, it just described the wrong thing — so
each case encodes a real clip and reads the real output back with ffprobe. Same reason
tests/test_stream_clamp.py measures pixels instead of flags.
"""

import shutil
import subprocess

import pytest

from app.services import media_service as M

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe are what this measures")


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=pix_fmt,profile,color_transfer,color_space", "-of", "default=nw=1", path],
        capture_output=True, timeout=30)
    got = {}
    for line in out.stdout.decode().splitlines():
        k, _, v = line.partition("=")
        got[k.strip()] = v.strip()
    return got


def _clip(path, *, hdr, tagged=True):
    """A two-second clip: 10-bit HLG like a phone writes, or plain 8-bit SDR."""
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi",
           "-i", "testsrc2=size=640x360:rate=15:duration=2", "-c:v", "libx264"]
    if hdr:
        cmd += ["-pix_fmt", "yuv420p10le", "-profile:v", "high10", "-colorspace", "bt2020nc"]
        if tagged:
            cmd += ["-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
                    "-x264-params", "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc"]
    else:
        cmd += ["-pix_fmt", "yuv420p"]
    subprocess.run(cmd + ["-y", path], check=True, capture_output=True, timeout=120)
    return path


@pytest.fixture(autouse=True)
def _no_gpu(monkeypatch):
    """CPU encoder only: the assertions are about colour and bit depth, and a CI box has no GPU.
    (The VAAPI path is asserted separately, by inspecting its command.)"""
    monkeypatch.setenv("VIDEO_HWACCEL", "0")
    M._video_encoder_cache = None
    yield
    M._video_encoder_cache = None


def test_the_source_really_is_the_thing_that_broke(tmp_path):
    """Guard the guard: if ffmpeg ever stops producing High 10 here, every assertion below would
    pass against a file that was never the problem."""
    got = _probe(_clip(str(tmp_path / "src.mp4"), hdr=True))
    assert got["profile"] == "High 10", got
    assert got["pix_fmt"] == "yuv420p10le", got
    assert got["color_transfer"] == "arib-std-b67", got


def test_hdr_comes_out_8_bit_and_bt709(tmp_path):
    """The whole bug, end to end."""
    src = _clip(str(tmp_path / "src.mp4"), hdr=True)
    out = str(tmp_path / "out.mp4")
    M.compress_video_file(src, out)
    got = _probe(out)
    assert got["pix_fmt"] == "yuv420p", f"still 10-bit — this will not play anywhere: {got}"
    assert "10" not in got["profile"], f"still High 10 — this will not play anywhere: {got}"
    assert got["color_transfer"] == "bt709", f"still HDR-tagged — the contrast bug: {got}"
    assert got["color_space"] == "bt709", got


def test_10_bit_with_no_tags_is_still_made_playable(tmp_path):
    """The case that took the first attempt down. zscale cannot convert from a transfer it was not
    told, so a bare `t=linear` fails with EINVAL and takes the ENTIRE encode with it — no video at
    all, which is worse than the bug being fixed. An untagged 10-bit source must still lose its
    depth (the unplayable half) without anyone trying to tonemap it."""
    src = _clip(str(tmp_path / "src.mp4"), hdr=True, tagged=False)
    out = str(tmp_path / "out.mp4")
    M.compress_video_file(src, out)          # must not raise
    got = _probe(out)
    assert got["pix_fmt"] == "yuv420p", got
    assert "10" not in got["profile"], got


def test_ordinary_sdr_video_is_left_exactly_as_it_was(tmp_path):
    """The other direction: this must not become a tonemap on every clip anybody posts."""
    src = _clip(str(tmp_path / "src.mp4"), hdr=False)
    info = M._probe_color("ffmpeg", src)
    assert not M._is_hdr_or_10bit(info), info
    cmd = M._video_encode_cmd("ffmpeg", "libx264", src, "/dev/null", "scale=w=640:h=-2", 23, "veryfast")
    assert cmd[cmd.index("-vf") + 1] == "scale=w=640:h=-2", "an SDR clip picked up a tonemap"
    out = str(tmp_path / "out.mp4")
    M.compress_video_file(src, out)
    assert _probe(out)["pix_fmt"] == "yuv420p"


def test_the_vaapi_chain_tonemaps_on_the_cpu_before_the_upload(tmp_path):
    """A GPU surface cannot be tonemapped by a CPU filter, so order is the whole thing here — and no
    GPU is needed to assert it."""
    src = _clip(str(tmp_path / "src.mp4"), hdr=True)
    cmd = M._video_encode_cmd("ffmpeg", "h264_vaapi", src, "/dev/null", "scale=w=640:h=-2", 23, "veryfast")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.endswith(",format=nv12,hwupload"), vf
    assert "tonemap" in vf and vf.index("tonemap") < vf.index("hwupload"), vf
    # nv12 is already 8-bit; naming -pix_fmt there fights the hwupload chain.
    assert "-pix_fmt" not in cmd, cmd
