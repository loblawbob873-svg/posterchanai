"""The `talk` puppet lip-sync: geometry, the audio envelope, and the wiring it has to reach.

Run: venv-unified/bin/python -m unittest tests.test_talk_lipsync

The renderer is driven with SYNTHETIC geometry rather than a detected face, so these run on a node
with no insightface weights — the detection is InsightFace's problem, the warp is ours.

Two of these pin defects that were actually hit while building it:
  * the cavity used to start ABOVE the lip seam, which painted its tooth strip onto the upper lip.
    On screen that is not "a mouth slightly too tall", it is a grey smear across the philtrum, and
    it looks like a broken effect rather than a cheap one.
  * frames_to_video used to do `frames = list(frames or [])`. A GENERATOR is always truthy, so the
    empty check silently passed and the whole clip was materialised in RAM (hundreds of MB for a
    long line). The lazy path has to keep raising on empty.
"""
import shutil
import unittest

from app.routers.telegram import messages as tg
from app.services.command_service import CommandService as CS
from app.services.effects_service import talk

try:
    import numpy as np
    from PIL import Image
    _HAVE_IMAGING = True
except Exception:                                        # pragma: no cover - imaging is a hard dep
    _HAVE_IMAGING = False

_HAVE_FFMPEG = bool(shutil.which("ffmpeg"))

# A face big enough that every fraction in the module rounds to real pixels.
CX, CY, MW, CHIN, ANGLE = 200.0, 150.0, 60.0, 42.0, 0.0


def _base():
    """A flat mid-grey canvas. Flat on purpose: any pixel that differs from 128 was written by the
    renderer, so 'what moved' is unambiguous."""
    return Image.new("RGB", (400, 300), (128, 128, 128))


def _render(openness):
    return list(talk._render_frames(_base(), CX, CY, MW, CHIN, ANGLE,
                                    openness, [0.5] * len(openness)))


@unittest.skipUnless(_HAVE_IMAGING, "numpy/Pillow unavailable")
class TestTheWarp(unittest.TestCase):
    def test_silence_is_the_untouched_picture(self):
        """A closed mouth must be the ORIGINAL frame, not a re-rendered approximation of it. The clip
        is mostly silence between words, so any drift here shows up as the picture flickering."""
        frame = _render([0.0])[0]
        self.assertEqual(list(frame.getdata()), list(_base().getdata()))

    def test_an_open_mouth_changes_the_mouth(self):
        frame = _render([1.0])[0]
        self.assertNotEqual(list(frame.getdata()), list(_base().getdata()))
        # ...and specifically at the mouth, not somewhere else on the picture.
        a = np.asarray(frame, dtype=np.int16)
        band = a[int(CY):int(CY + talk._JAW_DROP * CHIN), int(CX - MW / 2):int(CX + MW / 2)]
        self.assertGreater(int(np.abs(band - 128).max()), 20, "the mouth did not open")

    def test_nothing_is_painted_above_the_lip_seam(self):
        """The cavity is composited ON TOP of the picture, so an ellipse centred on the seam puts half
        of itself — including the tooth strip — over the upper lip and the nose."""
        frame = _render([1.0])[0]
        a = np.asarray(frame, dtype=np.int16)
        above = a[: int(CY - 0.35 * MW), :]
        self.assertEqual(int(np.abs(above - 128).max()), 0,
                         "the mouth cavity or the jaw reached above the lip seam")

    def test_the_face_is_left_alone_outside_the_jaw(self):
        """The jaw mask is bounded, so the rest of the picture — hair, background, the other people in
        a group shot — must come through untouched."""
        frame = _render([1.0])[0]
        a = np.asarray(frame, dtype=np.int16)
        far = a[:, : int(CX - 2.0 * MW)]
        self.assertEqual(int(np.abs(far - 128).max()), 0, "the warp leaked across the picture")

    def test_the_jaw_travel_scales_with_openness(self):
        """Half as loud must be visibly less open — otherwise the flap is a metronome and it stops
        reading as speech."""
        half, full = _render([0.45, 1.0])

        def _reach(frame):
            a = np.asarray(frame, dtype=np.int16)
            rows = np.where(np.abs(a - 128).max(axis=(1, 2)) > 0)[0]
            return int(rows.max() - rows.min()) if rows.size else 0

        self.assertGreater(_reach(full), _reach(half))


@unittest.skipUnless(_HAVE_IMAGING and _HAVE_FFMPEG, "ffmpeg/imaging unavailable")
class TestTheEnvelope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        import subprocess
        import tempfile
        cls.tmp = tempfile.mkdtemp(prefix="talktest_")
        cls.loud = os.path.join(cls.tmp, "loud.wav")
        cls.quiet = os.path.join(cls.tmp, "quiet.wav")
        # Half a second of tone, half a second of true silence, twice: speech has real gaps, and a
        # tremolo (which never reaches zero) would only prove the gate never fires.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "aevalsrc=sin(2*PI*200*t)*lt(mod(t\\,1)\\,0.5):d=2:s=16000", cls.loud],
                       check=True, timeout=120)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "anullsrc=r=16000:cl=mono", "-t", "2", cls.quiet],
                       check=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_envelope_is_the_clips_length(self):
        """One sample per video frame — this is also where add_talk gets its frame count, instead of
        asking ffprobe how long the audio is."""
        openness, width = talk._audio_envelope(self.loud, 20)
        self.assertEqual(len(openness), 40)          # 2s at 20fps
        self.assertEqual(len(width), 40)

    def test_it_stops_at_the_duration_cap(self):
        openness, _ = talk._audio_envelope(self.loud, 20, max_duration=0.5)
        self.assertEqual(len(openness), 10)

    def test_speech_opens_and_closes_the_mouth(self):
        openness, _ = talk._audio_envelope(self.loud, 20)
        self.assertGreater(float(openness.max()), 0.5, "a loud track never opened the mouth")
        self.assertLess(float(openness.min()), 0.5, "the mouth never closed between pulses")
        self.assertLessEqual(float(openness.max()), 1.0)

    def test_silence_keeps_the_mouth_shut(self):
        """The noise gate. Without it, room tone and mp3 ringing hold the jaw part-open for the whole
        clip and the face never stops mumbling."""
        openness, _ = talk._audio_envelope(self.quiet, 20)
        self.assertEqual(float(openness.max()), 0.0)

    def test_an_undecodable_track_is_an_error(self):
        """Deliberately NOT a flap-to-nothing fallback: whatever ffmpeg cannot read here it cannot mux
        either, so the 'fallback' would be a silent clip of a face chewing."""
        with self.assertRaises(RuntimeError):
            talk._audio_envelope("/nonexistent/nope.mp3", 20)


@unittest.skipUnless(_HAVE_IMAGING and _HAVE_FFMPEG, "ffmpeg/imaging unavailable")
class TestLazyFrameEncode(unittest.TestCase):
    def test_an_empty_generator_still_raises(self):
        from app.services.media_service import frames_to_video
        with self.assertRaises(RuntimeError):
            frames_to_video((f for f in []), fps=10)

    def test_a_generator_encodes(self):
        from app.services.media_service import frames_to_video
        out = frames_to_video((_base() for _ in range(4)), fps=4)
        self.assertGreater(len(out), 0)


class TestWiring(unittest.TestCase):
    """`talk` has to reach every interface. MEME_LAYER_TOOLS membership is covered by
    tests.test_meme_layer_tools; these are the surfaces that one does not look at."""

    def test_it_is_a_command_everywhere(self):
        self.assertIn("talk", CS.COMMANDS)
        self.assertIn("talk", CS.MEDIA_TOOL_COMMANDS)
        self.assertTrue(CS.wants_attachments("talk"))

    def test_telegram_matches_it_and_hands_it_the_bytes(self):
        # Telegram never calls parse_command — it matches its own literal list, and it OCRs an upload
        # for anything not named as a raw-media command. Missing either list is a silent failure: the
        # first falls through to the LLM (which would happily INVENT a talking video), the second
        # hands the command OCR'd text instead of a picture.
        #
        # NB this pins the wiring, not a working feature: Telegram cannot carry a photo AND an audio
        # clip in one message, and its handler downloads no audio at all — see docs/TALK.md. Being
        # matched is what makes it answer "attach a voice clip" instead of hallucinating.
        self.assertIn("talk", tg._TG_COMMANDS)
        self.assertIn("talk", tg._TG_RAW_MEDIA_COMMANDS)

    def test_the_render_lb_knows_talk_answers_with_raw_media(self):
        """A forwarded render answers with BYTES (a peer needs ffmpeg, not a blob store). A subpath
        missing from this tuple isn't a no-op — the LB hands the raw MP4 straight to the browser,
        which is waiting for {url,...}, so the layer silently never updates."""
        from app.routers import client as cl
        self.assertIn("talk", cl._MEME_RAW_MEDIA_SUBPATHS)

    def test_it_is_not_an_effect(self):
        """Effects read their argument as motion MODIFIERS, so `talk hello there` in an effect set
        would be parsed as two unknown modifiers instead of a line to say."""
        self.assertNotIn("talk", CS.MOTION_EFFECTS)
        self.assertNotIn("talk", CS.ANIMATED_EFFECTS)

    def test_it_speaks_with_the_cloned_voice_not_tts(self):
        """`talk` must go through voice_factory — the GPU-locked, load-balanced local model that
        `voice` uses — and NOT edge-tts. edge-tts is what `narrate` is for; wiring `talk` to it would
        silently give the meme a stock voice and bypass the whole GPU queue."""
        import inspect
        src = inspect.getsource(CS._talk_command)
        self.assertIn("voice_factory", src)
        self.assertNotIn("TTSService", src)

    def test_the_reference_clip_has_one_definition(self):
        """`voice` and `talk` normalise the reference the same way, through one helper — two copies
        would drift on the cap, the ffmpeg call and the wording of a bad clip."""
        self.assertTrue(callable(getattr(CS, "_voice_reference_wav", None)))
        import inspect
        for fn in (CS._voice_command, CS._talk_command):
            with self.subTest(command=fn.__name__):
                self.assertIn("_voice_reference_wav", inspect.getsource(fn))


if __name__ == "__main__":
    unittest.main()
