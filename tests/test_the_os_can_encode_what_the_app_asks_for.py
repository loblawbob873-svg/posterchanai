"""PosterChanOS must build an ffmpeg that can run the encoders this app actually invokes.

The operator was copying Jellyfin's ffmpeg binary over `/usr/bin/ffmpeg` by hand, on every machine,
because the packaged one could not do the job. Measured on a real node, Gentoo's ffmpeg had been
built with `dav1d lame libass opus theora truetype vpx` — **no x264 and no vaapi** — while the app
names `libx264` at 127 call sites and `h264_vaapi` at 66. So every encode fell back to software or
failed, and an `emerge ffmpeg` would silently take the hand-copied binary away again.

THE REQUIREMENT IS READ OUT OF THE APP, never typed here twice. A new encoder added to
`media_service` or `clamp.sh` joins this test's expectations by existing, which is the only version
of this test that cannot go stale — the failure it guards against is precisely the two halves
drifting apart.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENTOO_SH = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")

# Where the app decides what to run ffmpeg with.
SOURCES = ["app/services/media_service.py", "streamserver/clamp.sh",
           "app/services/effects_service/talk.py"]

# encoder/filter -> the ffmpeg USE flag that provides it. Only things the app can actually name.
PROVIDED_BY = {
    "libx264": "x264", "libx265": "x265",
    "h264_vaapi": "vaapi", "hevc_vaapi": "vaapi", "scale_vaapi": "vaapi",
    "h264_nvenc": "nvenc", "hevc_nvenc": "nvenc",
    "h264_amf": "amf",
    "libvpx": "vpx", "libvpx-vp9": "vpx",
    "libmp3lame": "lame", "libopus": "opus", "libvorbis": "vorbis",
    "zscale": "zimg", "drawtext": "truetype", "libwebp": "webp",
    "libsvtav1": "svt-av1",
}


def _app_text():
    out = []
    for rel in SOURCES:
        p = ROOT / rel
        if p.exists():
            out.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


def _ffmpeg_use():
    """Every flag ffmpeg ends up with: the image's GLOBAL USE plus its per-package line.

    Modelled the way portage resolves it, not just the per-package half — otherwise this test
    reports flags as missing that the image does supply (it did, for `lame` and `opus`, which live
    in USE_FLAGS). A flag switched OFF per-package wins over a global ON, so negations are applied
    last."""
    global_use = GENTOO_SH.split("USE_FLAGS=")[1].split("\n")[0]
    use = set(x for x in re.findall(r"[-\w.+]+", global_use) if not x.startswith("-"))
    m = re.search(r'"media-video/ffmpeg ([^"]+)"', GENTOO_SH)
    assert m, "the image sets no per-package USE for ffmpeg at all"
    for flag in m.group(1).split():
        use.discard(flag.lstrip("-"))
        if not flag.startswith("-"):
            use.add(flag)
    return use


def _ffmpeg_per_package():
    return set(re.search(r'"media-video/ffmpeg ([^"]+)"', GENTOO_SH).group(1).split())


class TheImageCanEncode(unittest.TestCase):

    def test_every_encoder_the_app_names_is_buildable(self):
        text, use = _app_text(), _ffmpeg_use()
        missing = {}
        for token, flag in PROVIDED_BY.items():
            if re.search(r"\b" + re.escape(token) + r"\b", text) and flag not in use:
                missing.setdefault(flag, []).append(token)
        self.assertEqual(missing, {}, "PosterChanOS builds an ffmpeg that cannot run: %s" % missing)

    def test_the_two_encoders_the_hardware_path_depends_on(self):
        """`_video_encoder_candidates` is NVENC → VAAPI → libx264. Lose vaapi and every node with
        no NVIDIA card transcodes on the CPU — a 3-hour stream at 46% of a core, measured."""
        use = _ffmpeg_use()
        for flag in ("vaapi", "x264"):
            self.assertIn(flag, use)

    def test_drawtext_and_zscale_are_available(self):
        """Both are FILTERS, so a missing one is not "no hardware acceleration" — it is ffmpeg
        exiting with "No such filter" on the watermark/outro every effect appends."""
        use = _ffmpeg_use()
        self.assertIn("truetype", use)
        self.assertIn("zimg", use)

    def test_webp_is_turned_on_for_ffmpeg_despite_being_off_globally(self):
        """The image sets `-webp` in USE_FLAGS. Per-package USE is the only thing that can give it
        to ffmpeg without rebuilding @world to get it."""
        self.assertIn("-webp", GENTOO_SH.split("USE_FLAGS=")[1].split("\n")[0])
        self.assertIn("webp", _ffmpeg_use())

    def test_the_codecs_live_in_make_conf_like_they_do_on_the_servers(self):
        """USE_FLAGS is what this script writes into the image's make.conf, and that is where the
        same codecs live on server1 and nas — one way of configuring all three machines."""
        global_use = GENTOO_SH.split("USE_FLAGS=")[1].split("\n")[0]
        for flag in ("x264", "vaapi", "zimg", "nvenc", "qsv", "amf"):
            self.assertIn(" " + flag + " ", global_use + " ",
                          flag + " is not in the image's make.conf USE")

    def test_only_webp_needs_a_per_package_override(self):
        """`-webp` is deliberately OFF globally in this image and ffmpeg is the one consumer that
        needs it, so it is the single exception — flipping it in USE_FLAGS would rebuild @world for
        one codec, and this repo has already lost a from-scratch build to a global flag (ABI_X86)."""
        self.assertIn("-webp", GENTOO_SH.split("USE_FLAGS=")[1].split("\n")[0])
        self.assertIn("webp", _ffmpeg_per_package())

    def test_every_flag_named_is_a_real_ffmpeg_use_flag(self):
        """A typo here is invisible: portage warns and carries on, and the encoder is simply absent
        on the built image. The list is ffmpeg-8.x's IUSE."""
        real = set("""X alsa amf amr amrenc appkit bluray bs2b bzip2 cairo cdio chromaprint chromium
            codec2 cuda-clang dav1d doc drm dvd fdk flite fontconfig frei0r fribidi gcrypt gme gmp
            gnutls gpl gsm iec61883 ieee1394 jack jpeg2k jpegxl kvazaar ladspa lame lcms libaom
            libaribb24 libass libcaca libilbc liblc3 libplacebo librtmp libsoxr lv2 lzma modplug
            nvenc ocr openal opencl opencolorio opengl openh264 openmpt openssl opus pulseaudio
            qrcode qsv quirc rabbitmq rav1e rist rubberband samba sdl snappy sndio soc speex srt ssh
            svg svt-av1 theora truetype twolame v4l vaapi vdpau vidstab vmaf vorbis vpx vulkan webp
            x264 x265 xml xvid zeromq zimg zlib zvbi""".split())
        for flag in _ffmpeg_per_package():
            self.assertIn(flag.lstrip("-"), real, "not an ffmpeg USE flag: " + flag)


if __name__ == "__main__":
    unittest.main()
