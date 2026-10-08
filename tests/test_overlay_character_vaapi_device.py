"""The corner-character overlay hands VAAPI its device, like every other encoder in media_service.

Found in the 2026-10-08 cleanup (pyflakes: `pre` assigned, never used). overlay_corner_character built
`pre = ["-vaapi_device", <render node>]` and then left it out of the command, so on an Intel/AMD node the VAAPI
attempt always died at `hwupload` ("a hardware device reference is required") and every overlay fell through to
the next encoder -- a wasted ffmpeg run per render, then the CPU. Runs the real function with ffmpeg stubbed.
"""
import subprocess

from app.services import media_service as ms


def test_the_vaapi_attempt_is_given_its_device_before_the_inputs(monkeypatch, tmp_path):
    char = tmp_path / "char.png"
    char.write_bytes(b"png")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        out = cmd[-1]
        open(out, "wb").write(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(ms, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(ms, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(ms, "_probe_width", lambda p: 640)
    monkeypatch.setattr(ms, "_probe_height", lambda p: 360)
    monkeypatch.setattr(ms, "_video_encoder_candidates", lambda f: ["h264_vaapi"])
    monkeypatch.setattr(ms, "_video_encoder_cache", None, raising=False)
    monkeypatch.setattr(ms, "_render_node", lambda: "/dev/dri/renderD128")
    monkeypatch.setattr(ms.subprocess, "run", run)
    assert ms.overlay_corner_character(b"video", "in.mp4", str(char)) == b"mp4"
    cmd = seen[0]
    assert "-vaapi_device" in cmd, cmd
    d = cmd.index("-vaapi_device")
    assert cmd[d + 1] == "/dev/dri/renderD128" and d < cmd.index("-i"), cmd
