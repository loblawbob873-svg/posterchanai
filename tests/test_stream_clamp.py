"""Tests for the live-stream bitrate clamp (app/services/stream_service.py + app/routers/streams.py).

Run: venv-unified/bin/python -m unittest tests.test_stream_clamp

The clamp re-encodes each live stream down to a ceiling so a streamer's OBS settings can't decide what
every viewer downloads. These cover the parts that fail SILENTLY (viewers just get the unclamped source,
which looks like it works) and the parts that are easy to "simplify" back into a bug:

- the generated MediaMTX config gives clamped paths their own entry with no runOnReady/record, which is
  what stops an infinite clamp-the-clamp chain and keeps VODs at source quality;
- RTSP is loopback + TCP-only (plain `rtsp: yes` also opens UDP :8000/:8001 on every interface);
- the generated shell script's ffmpeg args are quoted so the SHELL parses them (a fragment expanded from a
  variable keeps its quote characters as data and ffmpeg gets a filter string containing literal `"`);
- settings are validated, not escaped, before being interpolated into that script;
- the scale filter only ever downscales.

No MediaMTX, ffmpeg or database needed — this is all config/script generation and pure helpers.
"""
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import yaml

from app.services import stream_service as S


BASE = {
    "stream_enabled": "true",
    "stream_auth_secret": "hooksecret",
    "stream_rtmp_port": "1935",
    "stream_hls_port": "8888",
    "stream_api_port": "9997",
    "stream_rtsp_port": "8554",
}


def _render(tmpdir, **over):
    """Generate the config + clamp script into a temp dir and return (parsed_yaml, script_text)."""
    cfg = dict(BASE)
    cfg.update(over)
    with mock.patch.object(S, "_STREAM_CFG", tmpdir / "mediamtx.gen.yml"), \
         mock.patch.object(S, "_CLAMP_SCRIPT", tmpdir / "clamp.sh"):
        S._write_config(cfg)
        text = (tmpdir / "mediamtx.gen.yml").read_text()
        script = (tmpdir / "clamp.sh").read_text() if (tmpdir / "clamp.sh").exists() else ""
    return yaml.safe_load(text), script


class _TmpMixin:
    def setUp(self):
        import pathlib
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class TestGeneratedConfig(_TmpMixin, unittest.TestCase):
    def test_clamped_paths_get_their_own_entry(self):
        """No runOnReady on clamped paths, or MediaMTX clamps our clamp forever; no record, or every VOD is
        stored twice and stream_vod_service's <rec_dir>/<token>/ layout gains a bogus sibling."""
        doc, _ = _render(self.tmp, stream_record_enabled="true")
        key = f"~^.*{S.CLAMP_SUFFIX}$"
        self.assertIn(key, doc["paths"])
        entry = doc["paths"][key]
        self.assertNotIn("runOnReady", entry)
        self.assertNotIn("runOnNotReady", entry)
        self.assertIs(entry["record"], False)
        # ...while the SOURCE path does record and does trigger the clamp.
        self.assertIs(doc["paths"]["all_others"]["record"], True)
        self.assertIn("clamp.sh", doc["paths"]["all_others"]["runOnReady"])

    def test_regex_entry_precedes_all_others(self):
        """MediaMTX tries regex paths in config order before all_others; if all_others came first the
        clamped path would inherit runOnReady and recurse."""
        doc, _ = _render(self.tmp)
        keys = list(doc["paths"].keys())
        self.assertLess(keys.index(f"~^.*{S.CLAMP_SUFFIX}$"), keys.index("all_others"))

    def test_rtsp_is_loopback_and_tcp_only(self):
        doc, _ = _render(self.tmp)
        self.assertEqual(doc["rtsp"], True)
        self.assertTrue(str(doc["rtspAddress"]).startswith("127.0.0.1:"))
        # Without this MediaMTX also binds UDP :8000/:8001 on every interface.
        self.assertEqual(doc["rtspTransports"], ["tcp"])

    def test_clamp_off_opens_no_rtsp_and_no_transcode(self):
        doc, script = _render(self.tmp, stream_clamp_enabled="false")
        self.assertEqual(doc["rtsp"], False)
        self.assertNotIn("rtspAddress", doc)
        self.assertNotIn("runOnReady", doc["paths"]["all_others"])
        self.assertNotIn(f"~^.*{S.CLAMP_SUFFIX}$", doc["paths"])
        self.assertEqual(script, "")


class TestGeneratedScript(_TmpMixin, unittest.TestCase):
    def test_shell_syntax_is_valid(self):
        import subprocess
        _, script = _render(self.tmp)
        p = self.tmp / "check.sh"
        p.write_text(script)
        r = subprocess.run(["sh", "-n", str(p)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_filter_quotes_are_shell_parsed_not_data(self):
        """The encoder args must appear literally in the script. If they were expanded from a variable the
        shell would not re-parse the quotes and ffmpeg would receive a filter containing literal `"`."""
        import re
        _, script = _render(self.tmp)
        self.assertIn('-vf "fps=30,scale=w=', script)
        # The ffmpeg invocations must not take their encoder args from a positional parameter. ($1 does
        # appear elsewhere — it's the stream path the script is called with.)
        for line in script.splitlines():
            if line.lstrip().startswith("exec "):
                self.assertIsNone(re.search(r'\$\{?1\b', line), line)

    def test_publishes_with_the_clamp_secret(self):
        _, script = _render(self.tmp)
        self.assertIn(f"?clamp={S.clamp_secret(BASE)}", script)

    def test_refuses_to_clamp_a_clamped_path(self):
        _, script = _render(self.tmp)
        self.assertIn(f"*{S.CLAMP_SUFFIX})", script)

    def test_encoder_health_is_probed_not_inferred_from_runtime(self):
        """Measured in production, not hypothetical: a WHIP/phone publisher renegotiates a second or two
        after go-live, which kills the SOURCE and takes the transcode down with it. A "died early therefore
        the encoder is broken" rule reads that as hardware failure and demotes a perfectly good GPU stream
        to libx264 (46% of a core) for its entire duration. Runtime cannot tell those two apart."""
        _, script = _render(self.tmp)
        self.assertIn("hw_ok()", script)
        self.assertNotIn("-lt 10", script)          # no runtime-based demotion, at any threshold

    def test_probe_uses_the_real_encoder_arguments(self):
        """The probe must test the FULL argument set, not just -c:v. Rate-control flags are spelled
        differently per encoder and are rejected at open time, so probing a reduced set would pass and
        then the real encode would fail forever on every restart."""
        _, script = _render(self.tmp, stream_clamp_encoder="h264_vaapi")
        probe = script[script.index("hw_ok()"):script.index("START=")]
        for flag in ("-c:v h264_vaapi", "-rc_mode VBR", "-maxrate ${VMAX}k", "hwupload", "-f null -"):
            self.assertIn(flag, probe, flag)

    def test_effective_ceiling_is_computed_before_it_is_used(self):
        """$VMAX/$VBUF are referenced by run_hw/run_sw AND by the probe. If the measurement block were
        emitted after the first use the encoder would silently get an empty `-b:v k`."""
        _, script = _render(self.tmp)
        self.assertLess(script.index("VBUF=$((VMAX * 2))"), script.index("hw_ok; then"))
        self.assertLess(script.index("SRC_KBPS=$(measure_src_kbps)"), script.index("VBUF=$((VMAX * 2))"))

    def test_never_spends_more_than_the_source(self):
        """The ceiling is min(measured - audio, configured). Rate control alone cannot prevent inflating a
        weak source — re-encoding low-bitrate video is expensive because its artefacts are detail the
        encoder must reproduce. Measured on real phone video: 304 kbit/s in came out at 1447 with a fixed
        1500k ceiling, and at 239 (a REDUCTION) once the ceiling followed the source."""
        _, script = _render(self.tmp)
        self.assertIn("VMAX=$(( SRC_KBPS * 5 / 4 - AUD ))", script)              # 1.25x headroom; settle handles ramp
        self.assertIn('[ "$VMAX" -gt "$VMAX_CFG" ] && VMAX=$VMAX_CFG', script)   # never above configured
        self.assertIn('[ "$VMAX" -lt "$VMIN" ] && VMAX=$VMIN', script)           # never absurdly low

    def test_settles_before_measuring(self):
        """WebRTC bandwidth estimation ramps up over seconds, so the opening moments of a WHIP publish are
        not representative. Sampling them would pin a phone that later sends 2.5 Mbit/s to a fraction of
        that for the whole session — a stable stream never restarts, so it never re-measures."""
        _, script = _render(self.tmp)
        self.assertIn("SETTLE=15", script)
        self.assertLess(script.index('sleep "$SETTLE"'), script.index("SRC_KBPS=$(measure_src_kbps)"))

    def test_audio_bitrate_scales_with_the_source(self):
        """At a fixed 128k a 200 kbit/s stream spends two-thirds of its budget on audio and the picture is
        unwatchable. Cap audio at a quarter of the budget, floored at something still intelligible."""
        _, script = _render(self.tmp)
        self.assertIn("AUD=$((SRC_KBPS / 4))", script)
        self.assertIn('[ "$AUD" -lt "$AUD_MIN" ] && AUD=$AUD_MIN', script)
        self.assertIn('[ "$AUD" -gt "$AUD_CFG" ] && AUD=$AUD_CFG', script)
        self.assertIn("-b:a ${AUD}k", script)          # and the encode actually uses it

    def test_outopts_defined_after_the_audio_bitrate_exists(self):
        """OUTOPTS interpolates $AUD at assignment time and the script runs under `set -u`, so defining it
        before the measurement block would abort every clamp with an unbound variable."""
        _, script = _render(self.tmp)
        self.assertLess(script.index("AUD=$AUD_CFG"), script.index("OUTOPTS="))

    def test_measurement_remuxes_to_matroska_not_mp4(self):
        """A WHIP publisher can negotiate VP8, which mp4 cannot carry — the remux writes 0 bytes, the
        measurement reads 0, and the ceiling silently falls back to the configured value on exactly the
        streams this protects. Verified: `-f mp4` on VP8 gives 0 bytes, `-f matroska` gives a real file."""
        _, script = _render(self.tmp)
        self.assertIn("-c copy -f matroska", script)
        self.assertNotIn("-c copy -f mp4", script)

    def test_unmeasurable_source_falls_back_to_the_configured_ceiling(self):
        """Guessing low would visibly wreck a stream that is actually fine."""
        _, script = _render(self.tmp)
        self.assertIn("VMAX=$VMAX_CFG", script)

    def test_cpu_encoder_skips_the_probe(self):
        _, script = _render(self.tmp, stream_clamp_encoder="libx264")
        self.assertIn('[ "libx264" = "libx264" ]', script)

    def test_backs_off_instead_of_respawn_looping(self):
        """MediaMTX restarts runOnReady the instant it exits and applies no backoff, so a node with no
        usable ffmpeg would spin for the whole stream."""
        _, script = _render(self.tmp)
        self.assertIn("sleep 5", script)


class TestClampParams(unittest.TestCase):
    def test_scale_filter_caps_the_short_side(self):
        _, post = S._clamp_video_args("libx264", S._clamp_params(BASE))
        self.assertIn("min(720,iw)", post)      # portrait: cap width
        self.assertIn("min(720,ih)", post)      # landscape: cap height

    def test_every_encoder_forces_420_chroma(self):
        """Without an explicit format filter the encoder inherits the source pixel format, and a 4:4:4 input
        makes libx264 reject `-profile:v main` outright — breaking the CPU fallback path specifically."""
        p = S._clamp_params(BASE)
        for enc, expect in (("libx264", "format=yuv420p"), ("h264_nvenc", "format=nv12"),
                            ("h264_vaapi", "format=nv12")):
            self.assertIn(expect, S._clamp_video_args(enc, p)[1], enc)

    def test_vaapi_device_is_a_pre_input_option(self):
        """ffmpeg silently ignores -vaapi_device after -i, then fails to open the encoder."""
        pre, post = S._clamp_video_args("h264_vaapi", S._clamp_params(BASE))
        self.assertIn("-vaapi_device", pre)
        self.assertNotIn("-vaapi_device", post)

    def test_junk_settings_fall_back_to_defaults(self):
        """Settings sync between nodes over the relay, so they are not trusted input — and they are
        interpolated into a shell script."""
        p = S._clamp_params({
            "stream_clamp_height": "720; rm -rf /",
            "stream_clamp_fps": "-1",
            "stream_clamp_bitrate": "$(id)",
            "stream_clamp_audio_bitrate": "'; touch /tmp/pwned; '",
            "stream_clamp_encoder": "libx264 --evil",
        })
        self.assertEqual(p, {"height": "720", "fps": "30", "vbitrate": "1500k",
                             "abitrate": "128k", "encoder": ""})

    def test_valid_settings_are_kept(self):
        p = S._clamp_params({"stream_clamp_height": "1080", "stream_clamp_fps": "60",
                             "stream_clamp_bitrate": "3M", "stream_clamp_audio_bitrate": "96k",
                             "stream_clamp_encoder": "h264_nvenc"})
        self.assertEqual(p, {"height": "1080", "fps": "60", "vbitrate": "3M",
                             "abitrate": "96k", "encoder": "h264_nvenc"})

    def test_bitrate_is_a_ceiling_not_a_target(self):
        """Measured on the Arc: under ffmpeg's DEFAULT rate control, `-b:v 1500k` padded a 125 kbps phone
        source up to 1441 kbps — an 11.5x inflation, the opposite of the point, worst on the weakest
        connections. Each encoder needs its own capped-quality spelling, and the wrong one silently
        reverts to padding rather than erroring, so assert the exact flags."""
        p = S._clamp_params(BASE)
        self.assertIn("-rc_mode VBR", S._clamp_video_args("h264_vaapi", p)[1])
        self.assertIn("-crf 23", S._clamp_video_args("libx264", p)[1])
        nvenc = S._clamp_video_args("h264_nvenc", p)[1]
        self.assertIn("-rc vbr", nvenc)
        self.assertIn("-cq 24", nvenc)
        self.assertIn("-b:v 0", nvenc)          # a non-zero -b:v re-asserts a target and pads again

    def test_vbv_buffer_is_twice_the_ceiling(self):
        """A 1x buffer is effectively CBR and reintroduces the padding this is meant to remove."""
        for enc in ("h264_vaapi", "h264_nvenc", "libx264"):
            self.assertIn("-bufsize ${VBUF}k", S._clamp_video_args(enc, S._clamp_params(BASE))[1], enc)

    def test_rate_kbps_units(self):
        self.assertEqual(S._rate_kbps("1500k"), 1500)
        self.assertEqual(S._rate_kbps("3M"), 3000)
        self.assertEqual(S._rate_kbps("800"), 800)          # bare digits already read as kbit/s
        self.assertEqual(S._rate_kbps("junk"), 1500)        # unrecognised -> default, never crashes
        self.assertEqual(S._rate_kbps("", default=128), 128)

    def test_gop_tracks_fps_for_clean_segment_cuts(self):
        _, post = S._clamp_video_args("libx264", S._clamp_params({"stream_clamp_fps": "60"}))
        self.assertIn("-g 120", post)           # 2s at 60fps == hlsSegmentDuration


def _have_ffmpeg():
    import shutil
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@unittest.skipUnless(_have_ffmpeg(), "needs ffmpeg/ffprobe")
class TestScaleFilterAgainstRealFfmpeg(unittest.TestCase):
    """Run the generated scale filter through ffmpeg and check the ACTUAL output dimensions.

    This exists because asserting on the filter string is not enough: an earlier version asserted the
    string happily while portrait sources came out at 406x720 instead of 720x1280 — the string was as
    intended, the intent was wrong. Only measuring the pixels catches that class of bug.
    """

    def _clamped_size(self, size, height="720"):
        import re
        import subprocess
        import tempfile
        _, post = S._clamp_video_args("libx264", S._clamp_params({"stream_clamp_height": height}))
        vf = re.search(r'-vf "([^"]+)"', post).group(1)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as out:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", f"testsrc=size={size}:rate=30:duration=1", "-vf", vf,
                            "-c:v", "libx264", "-preset", "ultrafast", "-f", "mp4", "-y", out.name],
                           check=True, capture_output=True)
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", out.name],
                               check=True, capture_output=True, text=True)
        return r.stdout.strip().strip(",")

    def test_landscape_becomes_720p(self):
        self.assertEqual(self._clamped_size("1920x1080"), "1280,720")

    def test_portrait_becomes_720p_the_other_way_up(self):
        """720p means the SHORT side is 720. A plain height cap gives 406x720 here."""
        self.assertEqual(self._clamped_size("1080x1920"), "720,1280")

    def test_4k_comes_all_the_way_down(self):
        self.assertEqual(self._clamped_size("3840x2160"), "1280,720")

    def test_already_at_or_below_the_cap_is_untouched(self):
        for size in ("1280x720", "854x480", "640x480"):
            self.assertEqual(self._clamped_size(size), size.replace("x", ","), size)

    def test_honours_a_custom_height(self):
        self.assertEqual(self._clamped_size("1920x1080", height="480"), "854,480")


class TestClampSecret(unittest.TestCase):
    def test_deterministic_and_derived_from_the_hook_secret(self):
        a = S.clamp_secret({"stream_auth_secret": "abc"})
        self.assertEqual(a, S.clamp_secret({"stream_auth_secret": "abc"}))
        self.assertNotEqual(a, S.clamp_secret({"stream_auth_secret": "abd"}))
        self.assertNotEqual(a, "abc")           # never exposes its parent

    def test_empty_without_a_hook_secret(self):
        """An empty secret must not authorize anything — /api/streams/auth requires a truthy `want`."""
        self.assertEqual(S.clamp_secret({"stream_auth_secret": ""}), "")


class TestSettingsContract(unittest.TestCase):
    """Every clamp setting must live on the relay and hydrate like any other admin setting.

    This is a silent failure mode: a key missing from SettingsResponse still "works" locally off the
    inline default, it just never persists to Nostr and never syncs to the other nodes — so the admin
    changes it, sees it take effect, and finds it reverted after a restart.
    """

    KEYS = ("stream_clamp_enabled", "stream_clamp_height", "stream_clamp_fps", "stream_clamp_bitrate",
            "stream_clamp_audio_bitrate", "stream_clamp_encoder", "stream_rtsp_port")

    def test_all_are_declared_in_the_settings_schema(self):
        from app.schemas import SettingsResponse
        for k in self.KEYS:
            self.assertIn(k, SettingsResponse.model_fields, k)

    def test_none_are_local_only(self):
        """Local-only keys (plumbing / per-node cursors) never reach the relay. These are shareable."""
        from app.services import settings_store
        for k in self.KEYS:
            self.assertFalse(settings_store._is_local_only(k), k)

    def test_schema_defaults_match_the_documented_ceiling(self):
        from app.schemas import SettingsResponse
        f = SettingsResponse.model_fields
        self.assertEqual(f["stream_clamp_enabled"].default, "true")   # ON by default is the whole point
        self.assertEqual(f["stream_clamp_height"].default, "720")
        self.assertEqual(f["stream_clamp_fps"].default, "30")
        self.assertEqual(f["stream_clamp_bitrate"].default, "1500k")

    def test_all_are_seeded_as_startup_defaults(self):
        """Seeded in database.py so a fresh node publishes them to its relay instead of relying on the
        schema fallback (which would leave the Admin form showing values the relay doesn't hold)."""
        import pathlib
        import re
        src = pathlib.Path("app/database.py").read_text()
        block = src[src.index("default_settings = {"):]
        for k in self.KEYS:
            self.assertRegex(block, rf'"{re.escape(k)}"\s*:', k)

    def test_blank_values_are_survivable(self):
        """Admin can clear any of these (they're all str, so the API treats '' as a real CLEAR); the
        validator must fall back to defaults rather than emit an empty ffmpeg argument."""
        p = S._clamp_params({k: "" for k in self.KEYS})
        self.assertEqual(p["height"], "720")
        self.assertEqual(p["vbitrate"], "1500k")


class TestClampEnabled(unittest.TestCase):
    def test_defaults_on(self):
        self.assertTrue(S.clamp_enabled({}))

    def test_admin_can_turn_it_off(self):
        self.assertFalse(S.clamp_enabled({"stream_clamp_enabled": "false"}))


if __name__ == "__main__":
    unittest.main()


class TestPerStreamerQualityTiers(unittest.TestCase):
    """A streamer may LOWER their own stream's quality; they may never raise it.

    The generated script is shell, so the two failure modes worth pinning are (a) a tier whose args got
    mangled by quoting — the reason each tier gets its own run function with LITERAL args rather than a
    variable — and (b) a tier that resolves higher than the admin ceiling, which would turn a "save my
    data" control into a way to make the node spend more.
    """

    CFG = {"stream_clamp_height": "720", "stream_clamp_fps": "30", "stream_clamp_bitrate": "1500k",
           "stream_clamp_audio_bitrate": "128k", "stream_clamp_encoder": "libx264",
           "stream_rtsp_port": "8554", "stream_auth_secret": "s3cret"}

    def _script(self, cfg=None):
        S._write_clamp_script(cfg or dict(self.CFG))
        return Path(str(S._CLAMP_SCRIPT)).read_text()

    def test_the_generated_script_is_valid_shell(self):
        self._script()
        r = subprocess.run(["sh", "-n", str(S._CLAMP_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_tier_has_its_own_run_functions(self):
        t = self._script()
        for name in S.QUALITY_TIERS:
            self.assertIn(f"run_hw_{name}()", t)
            self.assertIn(f"run_sw_{name}()", t)
        self.assertIn("run_hw_auto()", t)          # a stream with no tier must still have its path
        self.assertIn("run_sw_auto()", t)

    def test_the_quality_path_is_a_real_path_not_a_python_repr(self):
        """`{quality_dir}` in the script f-string interpolated the FUNCTION object, and sh -n accepts that
        happily because it is syntactically a fine string — so the tier file could never be found."""
        t = self._script()
        line = [l for l in t.splitlines() if l.startswith("QFILE=")][0]
        self.assertNotIn("<function", line)
        self.assertIn("/quality/", line)

    def test_a_tier_can_only_lower_the_ceiling(self):
        """With the node configured BELOW a tier, that tier must not raise it."""
        cfg = dict(self.CFG)
        cfg["stream_clamp_bitrate"] = "400k"       # lower than every tier's own rate
        cfg["stream_clamp_height"] = "360"
        t = self._script(cfg)
        for line in t.splitlines():
            if ") VMAX_CFG=" in line:
                kbps = int(line.split("VMAX_CFG=")[1].split()[0].rstrip(";").strip())
                self.assertLessEqual(kbps, 400, line)
        # …and no tier's filter may ask for more height than the node allows
        self.assertNotIn("min(720,", t)
        self.assertNotIn("min(480,", t)

    def test_tier_scale_filters_survive_generation_intact(self):
        """The filter carries nested single quotes; each tier's must arrive complete, with its own height."""
        t = self._script()
        self.assertIn("min(480,iw)", t)
        self.assertIn("min(360,iw)", t)
        self.assertIn("min(720,iw)", t)

    def test_set_and_get_round_trip_and_reject_bad_tokens(self):
        self.assertEqual(S.set_quality("tok_ABC-1", "480"), "480")
        self.assertEqual(S.get_quality("tok_ABC-1"), "480")
        # anything unknown clears back to auto rather than leaving a stale file
        self.assertEqual(S.set_quality("tok_ABC-1", "9001"), "auto")
        self.assertEqual(S.get_quality("tok_ABC-1"), "auto")
        for bad in ("", "../etc/passwd", "a b", "a;rm -rf /", "x" * 200):
            with self.assertRaises(ValueError):
                S.set_quality(bad, "480")
            self.assertEqual(S.get_quality(bad), "auto")
