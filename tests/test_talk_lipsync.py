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
import io
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


@unittest.skipUnless(_HAVE_IMAGING, "imaging unavailable")
class TestTheMouthInterior(unittest.TestCase):
    """The inside of the mouth on a PHOTOGRAPH, which is the half that used to look pasted-on.

    The reported symptom was "you can see their teeth and it looks weird". Measured on a real
    photo the tooth strip composited at 1.14x the luminance of the cheek beside it — the inside
    of the mouth was BRIGHTER than the face, which no real mouth ever is. It came from a fixed
    (214,206,194) fill that ignored the picture's exposure entirely; on a dark-skinned face the
    same constant landed at 2.85x. The anime renderer never had the bug because it draws no
    teeth at all.
    """

    # "mid" is the skin actually MEASURED off the face this was reported on (insightface's own
    # t1.jpg sample), luminance 124. It is not a decorative choice: an earlier version of this test
    # used a lighter mid-tone at luminance 163, and since the old tooth strip composited to a fixed
    # ~141 whatever the picture, that swatch sailed past the assertion and the test only caught the
    # bug on dark skin. Pick swatches that bracket the reported case, not ones that flatter it.
    SKINS = [("dark", (92, 66, 56)), ("mid", (155, 114, 97)), ("bright", (244, 218, 202))]

    def _interior_stats(self, skin, openness=1.0, mw=60.0, chin=42.0):
        """Brightest and mean luminance INSIDE the cavity, composited over the skin — i.e. what
        the viewer actually sees, after the rim blur."""
        drop = openness * talk._JAW_DROP * chin
        patch, _off = talk._mouth_interior(mw, drop, 0.5, 0.0, skin=skin, openness=openness)
        bg = Image.new("RGB", patch.size, skin)
        bg.paste(patch, (0, 0), patch)
        arr = np.asarray(bg, dtype=np.float32)
        lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
        # Rows are picked by the patch's own ALPHA, never by brightness: a luminance threshold
        # would silently exclude the bright teeth this test exists to catch (it did, once).
        alpha = np.asarray(patch.split()[3], dtype=np.float32)
        cx = patch.width // 2
        rows = np.where(alpha[:, cx - 2:cx + 3].mean(axis=1) > 140)[0]
        self.assertTrue(rows.size, "no cavity was drawn at all")
        col = lum[:, cx - 2:cx + 3].mean(axis=1)[rows]
        return float(col.max()), float(col.mean())

    @staticmethod
    def _lum(rgb):
        return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]

    def test_the_interior_is_never_brighter_than_the_face(self):
        """THE defect. Teeth are in shadow under the top lip, so however the numbers are tuned the
        brightest pixel inside the mouth has to stay under the lit cheek — at every exposure and
        every openness, because a constant that happens to look right on one skin tone is exactly
        how this broke."""
        for name, skin in self.SKINS:
            for openness in (0.3, 0.65, 1.0):
                with self.subTest(skin=name, openness=openness):
                    brightest, _mean = self._interior_stats(skin, openness)
                    self.assertLess(brightest, self._lum(skin),
                                    f"the mouth interior out-shines the face on {name} skin")

    def test_the_interior_follows_the_photos_exposure(self):
        """It has to be DERIVED from the picture, not a palette. A fixed fill is what made one
        constant read as grey teeth on a bright face and glowing teeth on a dark one."""
        dark, _ = self._interior_stats(self.SKINS[0][1])
        bright, _ = self._interior_stats(self.SKINS[2][1])
        self.assertGreater(bright, dark * 1.2,
                           "the interior ignored the picture's exposure")

    def test_a_dark_face_still_gets_a_mouth_and_not_a_void(self):
        """The other failure mode, and the first fix hit it: scaling everything at a flat fraction
        of skin luminance drove a dark face's tongue to (32,15,16) and left a featureless black
        slot. Enamel and tongue are their own materials — the cavity floor and the brightness bias
        are what keep the mouth from reading as a hole punched in the face."""
        skin = self.SKINS[0][1]
        brightest, mean = self._interior_stats(skin, openness=1.0)
        self.assertGreater(brightest, mean * 1.25,
                           "the cavity is flat — no teeth or tongue survived")

    def test_a_barely_open_mouth_does_not_flash_its_teeth(self):
        """The strip used to arrive at full strength the moment the cavity cleared 4px, so quiet
        syllables strobed. It fades in with the vowel instead."""
        skin = self.SKINS[1][1]
        quiet, _ = self._interior_stats(skin, openness=0.25)
        loud, _ = self._interior_stats(skin, openness=1.0)
        self.assertLess(quiet, loud, "the teeth are at full brightness on a near-closed mouth")


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


@unittest.skipUnless(_HAVE_IMAGING, "numpy/Pillow unavailable")
class TestTransparency(unittest.TestCase):
    """A Meme Builder layer COMPOSITES, so a background-removed cut-out has to stay cut out.

    MP4 has no alpha channel at all: rendering one turned such a layer into a black rectangle with
    the subject pasted on top — the reported bug. The transparent form has to be a VP9-alpha WebM,
    and that form has to be SILENT, because an audio stream in one corrupts the alpha on this ffmpeg
    (media_service._ALPHA_VCODEC). The voice becomes its own timeline layer instead.
    """

    def test_it_detects_real_transparency_only(self):
        from PIL import Image
        opaque_rgba = Image.new("RGBA", (8, 8), (10, 20, 30, 255))
        cut_out = Image.new("RGBA", (8, 8), (10, 20, 30, 255))
        cut_out.putpixel((0, 0), (0, 0, 0, 0))
        self.assertFalse(talk._has_alpha(Image.new("RGB", (8, 8), (1, 2, 3))))
        self.assertFalse(talk._has_alpha(opaque_rgba), "an unused alpha channel is not transparency")
        self.assertTrue(talk._has_alpha(cut_out))

    def test_add_talk_reports_which_container_it_produced(self):
        """It returns (bytes, content_type) — the caller cannot tell webm from mp4 otherwise, and
        that flag is what makes the client add the separate audio layer."""
        import inspect
        sig = inspect.signature(talk.add_talk)
        self.assertIn("keep_alpha", sig.parameters)
        self.assertFalse(sig.parameters["keep_alpha"].default,
                         "chat/Telegram need ONE self-contained file, so the default stays MP4")

    def test_the_meme_endpoint_asks_to_keep_alpha(self):
        """The whole point of the fix. A Meme Builder layer composites; a chat reply does not."""
        import inspect
        from app.routers import client as cl
        src = inspect.getsource(cl.meme_talk)
        self.assertIn("add_talk", src)
        self.assertIn("alpha", src)

    def test_chat_delivery_stays_one_file(self):
        """talk_attachments must NOT hand back the silent transparent clip: a chat reply that is a
        video with no sound is the feature failing quietly."""
        import inspect
        src = inspect.getsource(talk.talk_attachments)
        self.assertIn("add_talk(data, audio_path)", src)
        self.assertNotIn("keep_alpha", src)


@unittest.skipUnless(_HAVE_IMAGING, "numpy/Pillow unavailable")
class TestHandPlacedMouth(unittest.TestCase):
    """Detection cannot be trusted on the art people actually meme with — InsightFace detects an
    anime face and then puts the mouth landmarks on the chin and a cheek. So the person looking at
    the picture gets the final say, and that placement has to survive every resize and be safe to
    take from a browser."""

    def test_detection_answers_in_normalised_coordinates(self):
        """Normalised, so the answer means the same thing whether the client is showing the picture
        at 300px or the renderer is working on it at 960."""
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (640, 480), (30, 30, 30)).save(buf, format="PNG")
        got = talk.detect_mouth(buf.getvalue())
        self.assertIn("found", got)
        self.assertFalse(got["found"], "a blank image has no face")
        for k in ("x", "y", "w", "angle", "anime"):
            self.assertIn(k, got)
        self.assertTrue(0.0 <= got["x"] <= 1.0 and 0.0 <= got["y"] <= 1.0)

    def test_a_placement_replaces_detection(self):
        import inspect
        sig = inspect.signature(talk.add_talk)
        self.assertIn("mouth", sig.parameters)
        self.assertIsNone(sig.parameters["mouth"].default,
                          "chat/Telegram have no picker, so they must still auto-detect")
        src = inspect.getsource(talk.add_talk)
        self.assertIn("_face_geometry", src)
        self.assertIn("is_anime = bool(mouth", src, "the placement also picks warp vs redraw")

    def test_the_placement_is_clamped(self):
        """It is untrusted input that becomes ellipse dimensions inside a 600-iteration render loop.
        `w` is what every length scales off, so an unclamped 0.9 would build canvases the size of
        the picture, per frame."""
        from app.routers.client import _clean_mouth
        self.assertIsNone(_clean_mouth(None))
        self.assertIsNone(_clean_mouth("nope"))
        wild = _clean_mouth({"x": 99, "y": -5, "w": 40, "angle": 999, "anime": 1})
        self.assertLessEqual(wild["w"], 0.6)
        self.assertEqual(wild["x"], 1.0)
        self.assertEqual(wild["y"], 0.0)
        self.assertLessEqual(abs(wild["angle"]), 45.0)
        self.assertIs(wild["anime"], True)
        junk = _clean_mouth({"x": "abc", "w": None})
        self.assertTrue(0.0 <= junk["x"] <= 1.0 and 0.01 <= junk["w"] <= 0.6)


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

    def test_a_character_pose_can_be_placed_like_any_other_face(self):
        """A pose's mouth is picked, not decided for you. The three server halves of that must agree
        on ONE name check, or the picker shows a picture the render doesn't animate (or 404s while
        the render happily proceeds): the artwork endpoint, the detection seed and the render all
        resolve through _pose_art_path."""
        from app.routers import client as cl
        self.assertTrue(cl._pose_art_path("carl"))
        self.assertTrue(cl._pose_art_path("Carl"))          # the layer stores whatever the catalogue said
        self.assertFalse(cl._pose_art_path("lookingaway"))  # an animation, not a pose — cannot talk
        self.assertFalse(cl._pose_art_path("../../etc/passwd"))
        self.assertFalse(cl._pose_art_path(""))
        import inspect
        for fn in (cl.meme_character_art, cl.meme_face, cl.meme_talk):
            with self.subTest(endpoint=fn.__name__):
                self.assertIn("_pose_art_path", inspect.getsource(fn))

    def test_the_pose_placement_reaches_the_render(self):
        """The client's pose branch must send `mouth` too. Dropping it doesn't fail — the server
        falls back to auto-detect — so the picker would appear, be dragged, and be ignored."""
        import os
        import re
        js = open(os.path.join(os.path.dirname(__file__), "..", "static", "js",
                               "client", "meme.js"), encoding="utf-8").read()
        # the pose body of the /meme/talk POST
        m = re.search(r"\{ pubkey: ME\.pubkey, auth, audio, character: pose[^}]*\}", js)
        self.assertIsNotNone(m, "the pose talk request went away")
        self.assertIn("mouth", m.group(0))
        # …and the picker is not skipped for it
        self.assertIn("pickMouth(l.src, pose)", js)

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
