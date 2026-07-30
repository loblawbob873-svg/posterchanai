"""Meme Builder: GIF/PNG export, clip speed, and the crossfade ramps.

Run: venv-unified/bin/python -m unittest tests.test_meme_export_speed_xfade

These three share one property that makes them worth real renders rather than string checks: each one
is a way for ffmpeg to accept the command and produce something WRONG rather than fail.

  * a GIF written by the h264 ladder is an MP4 named .gif — the container has to be asserted
  * `-t dur` with `setpts=PTS/2` gives a clip that ends halfway through its slot and freezes; the
    source length has to scale with the speed, which is invisible in a filter-string test
  * `atempo` silently REFUSES a factor outside 0.5-2.0, leaving 1x audio under a 4x picture, so the
    chained spelling is asserted explicitly
  * an alpha `fade` on a stream that is not rgba is a no-op, so the dissolve is measured on pixels
"""
import os
import subprocess
import tempfile
import unittest

from PIL import Image

from app.services import meme_builder_service as mb


def _probe(path, entries, stream="v:0"):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
                        "-show_entries", entries, "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, timeout=30)
    return (p.stdout or "").split()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not mb.media_service.resolve_ffmpeg():
            raise unittest.SkipTest("ffmpeg is not available on this node")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcmemefmt-")

    def tearDown(self):
        for f in os.listdir(self.tmp):
            try:
                os.unlink(os.path.join(self.tmp, f))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def _still(self, name, colour, size=(600, 400)):
        p = os.path.join(self.tmp, name)
        Image.new("RGB", size, colour).save(p)
        return p

    def _clip(self, name, colour, secs=2.0, rate=25):
        """A real video file, so the speed path has something with timestamps to restretch."""
        p = os.path.join(self.tmp, name)
        subprocess.run([mb.media_service.resolve_ffmpeg(), "-y", "-v", "error",
                        "-f", "lavfi", "-i", f"color=c={colour}:s=320x240:r={rate}:d={secs}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", p],
                       check=True, capture_output=True, timeout=60)
        return p

    def _write(self, data, name):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p


class TestExportFormats(_Base):
    def _one_layer_edit(self, **extra):
        e = {"w": 320, "h": 240, "fps": 20, "bg": "#000000", "duration": 1.0,
             "layers": [{"type": "image", "src": "S", "start": 0, "dur": 1.0,
                         "x": 0, "y": 0, "w": 320, "h": 240, "opacity": 1, "fit": "cover"}]}
        e.update(extra)
        return e

    def test_mp4_is_the_default_and_reports_its_type(self):
        data, ctype = mb.render(self._one_layer_edit(), {"S": self._still("a.jpg", (10, 120, 40))})
        self.assertEqual(ctype, "video/mp4")
        self.assertEqual(_probe(self._write(data, "o.mp4"), "stream=codec_name"), ["h264"])

    def test_gif_is_really_a_gif(self):
        """Not "the render succeeded": the h264 ladder would happily write an MP4 to out.gif."""
        data, ctype = mb.render(self._one_layer_edit(fmt="gif"),
                                {"S": self._still("a.jpg", (10, 120, 40))})
        self.assertEqual(ctype, "image/gif")
        self.assertTrue(data[:6] in (b"GIF87a", b"GIF89a"), data[:6])
        self.assertEqual(_probe(self._write(data, "o.gif"), "stream=codec_name"), ["gif"])

    def test_gif_long_edge_is_capped(self):
        """A full-size GIF is the thing that makes "export a GIF" unusable, so the cap is the feature."""
        e = self._one_layer_edit(fmt="gif", w=1080, h=1920)
        e["layers"][0].update({"w": 1080, "h": 1920})
        data, _ = mb.render(e, {"S": self._still("a.jpg", (10, 120, 40), size=(1080, 1920))})
        wh = _probe(self._write(data, "o.gif"), "stream=width,height")
        self.assertEqual(wh[1], str(mb.GIF_MAX_EDGE))     # portrait → the HEIGHT is the long edge
        self.assertLessEqual(int(wh[0]), mb.GIF_MAX_EDGE)

    def test_gif_actually_animates(self):
        """THE assertion the first version of these tests was missing, and the bug it would have caught:
        the one-pass split/palettegen/paletteuse graph produced a GIF with the right container, the right
        frame count and the right duration in which every frame was the FIRST one. Container and frame
        count are not animation — compare the pixels at each end."""
        e = {"w": 240, "h": 180, "fps": 25, "bg": "#000000", "duration": 3.0, "fmt": "gif",
             "layers": [
                 {"type": "image", "src": "A", "start": 0, "dur": 1.5,
                  "x": 0, "y": 0, "w": 240, "h": 180, "opacity": 1, "fit": "cover"},
                 {"type": "image", "src": "B", "start": 1.5, "dur": 1.5,
                  "x": 0, "y": 0, "w": 240, "h": 180, "opacity": 1, "fit": "cover"},
             ]}
        data, _ = mb.render(e, {"A": self._still("a.jpg", (255, 0, 0)),
                                "B": self._still("b.jpg", (0, 0, 255))})
        gif = self._write(data, "o.gif")
        # Frame count still has to be right (that part was never wrong). nb_read_frames needs
        # -count_frames or ffprobe reports N/A, so this goes through its own probe.
        cf = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                             "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", gif],
                            capture_output=True, text=True, timeout=30).stdout.strip()
        self.assertGreater(int(cf or 0), 1, "a GIF of one frame is not an animation")
        # … and the two ends must differ, red then blue. Read with ffmpeg, NOT with PIL's GIF seek:
        # PIL composites partial frames itself and reported red for both ends on a GIF that was correct.
        cols = []
        for t in ("0.4", "2.4"):
            f = os.path.join(self.tmp, f"g{t}.png")
            subprocess.run([mb.media_service.resolve_ffmpeg(), "-y", "-v", "error", "-ss", t,
                            "-i", gif, "-frames:v", "1", f], check=True, capture_output=True, timeout=30)
            with Image.open(f) as im:
                cols.append(im.convert("RGB").getpixel((120, 90)))
        self.assertGreater(cols[0][0], 150, cols)      # first half is red
        self.assertLess(cols[0][2], 90, cols)
        self.assertGreater(cols[1][2], 150, cols)      # second half is blue
        self.assertLess(cols[1][0], 90, cols)

    def test_gif_has_no_audio_stream(self):
        e = self._one_layer_edit(fmt="gif")
        e["layers"].append({"type": "text", "text": "hi", "start": 0, "dur": 1, "x": 10, "y": 10,
                            "size": 30, "sound": "gong"})
        data, _ = mb.render(e, {"S": self._still("a.jpg", (10, 120, 40))})
        self.assertEqual(_probe(self._write(data, "o.gif"), "stream=codec_type", stream="a"), [])

    def test_gif_refuses_a_long_project_with_a_usable_message(self):
        e = self._one_layer_edit(fmt="gif", duration=mb.MAX_GIF_DURATION + 5)
        e["layers"][0]["dur"] = mb.MAX_GIF_DURATION + 5
        with self.assertRaises(ValueError) as cm:
            mb.render(e, {"S": self._still("a.jpg", (10, 120, 40))})
        self.assertIn("MP4", str(cm.exception))            # says what to do instead

    def test_png_is_one_frame_taken_at_the_asked_for_time(self):
        """Two layers, one after the other; the still at 1.5s must be the SECOND one's colour."""
        e = {"w": 200, "h": 200, "fps": 20, "bg": "#000000", "duration": 2.0, "fmt": "png",
             "still": 1.5,
             "layers": [
                 {"type": "image", "src": "A", "start": 0, "dur": 1.0,
                  "x": 0, "y": 0, "w": 200, "h": 200, "opacity": 1, "fit": "cover"},
                 {"type": "image", "src": "B", "start": 1.0, "dur": 1.0,
                  "x": 0, "y": 0, "w": 200, "h": 200, "opacity": 1, "fit": "cover"},
             ]}
        data, ctype = mb.render(e, {"A": self._still("a.jpg", (255, 0, 0)),
                                    "B": self._still("b.jpg", (0, 0, 255))})
        self.assertEqual(ctype, "image/png")
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        px = Image.open(self._write(data, "o.png")).convert("RGB").getpixel((100, 100))
        self.assertLess(px[0], 60, px)      # not the red layer
        self.assertGreater(px[2], 180, px)  # the blue one

    def test_png_time_past_the_end_still_yields_a_frame(self):
        e = self._one_layer_edit(fmt="png", still=99)
        data, ctype = mb.render(e, {"S": self._still("a.jpg", (10, 200, 40))})
        self.assertEqual(ctype, "image/png")
        with Image.open(self._write(data, "o.png")) as im:
            self.assertEqual(im.size, (320, 240))

    def test_an_unknown_format_is_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            mb.render(self._one_layer_edit(fmt="webm"), {"S": self._still("a.jpg", (1, 2, 3))})


class TestClipSpeed(_Base):
    def _speed_edit(self, speed, slot=1.0):
        return {"w": 160, "h": 120, "fps": 20, "bg": "#000000", "duration": slot,
                "layers": [{"type": "video", "src": "V", "start": 0, "dur": slot, "trim": 0,
                            "x": 0, "y": 0, "w": 160, "h": 120, "opacity": 1, "speed": speed,
                            "mute": True, "fit": "cover"}]}

    def test_a_sped_up_clip_fills_its_whole_slot(self):
        """The regression this guards: with `-t dur` instead of `-t dur*speed`, a 2x clip runs out at
        half the slot and the overlay freezes on its last frame — which looks like a broken clip, not
        like a speed setting."""
        src = self._clip("v.mp4", "red", secs=4.0)
        data, _ = mb.render(self._speed_edit(2.0, slot=2.0), {"V": src})
        out = self._write(data, "o.mp4")
        dur = float(_probe(out, "stream=duration")[0])
        self.assertAlmostEqual(dur, 2.0, delta=0.35)

    def test_speed_is_ignored_on_a_still(self):
        """An image has no timeline of its own — `setpts` on a looped still would stretch nothing and
        `-t dur*speed` would make the slot the wrong length."""
        e = {"w": 160, "h": 120, "fps": 20, "bg": "#000000", "duration": 1.0,
             "layers": [{"type": "image", "src": "S", "start": 0, "dur": 1.0, "speed": 4.0,
                         "x": 0, "y": 0, "w": 160, "h": 120, "opacity": 1, "fit": "cover"}]}
        data, _ = mb.render(e, {"S": self._still("a.jpg", (9, 9, 200))})
        dur = float(_probe(self._write(data, "o.mp4"), "stream=duration")[0])
        self.assertAlmostEqual(dur, 1.0, delta=0.3)

    def test_audio_keeps_up_with_the_picture_at_4x(self):
        """atempo caps at 2.0 per instance and silently refuses more, so 4x has to be chained. The
        symptom of getting this wrong is audio still playing at 1x under a 4x picture."""
        src = self._clip("v.mp4", "blue", secs=8.0)
        e = self._speed_edit(4.0, slot=2.0)
        e["layers"][0]["mute"] = False
        data, _ = mb.render(e, {"V": src})
        out = self._write(data, "o.mp4")
        a = _probe(out, "stream=codec_type", stream="a")
        self.assertEqual(a, ["audio"])
        adur = float(_probe(out, "stream=duration", stream="a")[0])
        self.assertAlmostEqual(adur, 2.0, delta=0.4)

    def test_slow_motion_also_fills_the_slot(self):
        src = self._clip("v.mp4", "green", secs=2.0)
        data, _ = mb.render(self._speed_edit(0.5, slot=2.0), {"V": src})
        dur = float(_probe(self._write(data, "o.mp4"), "stream=duration")[0])
        self.assertAlmostEqual(dur, 2.0, delta=0.35)


class TestCrossfade(_Base):
    def test_overlapping_clips_dissolve(self):
        """Red for 2s and blue starting at 1.5s, with a 0.5s ramp on each. Mid-overlap the frame must be
        a MIX — both channels present — where a hard cut would be pure blue and no ramp at all would be
        pure blue too (the later layer draws on top). That is why this is measured in pixels."""
        e = {"w": 120, "h": 120, "fps": 25, "bg": "#000000", "duration": 3.0,
             "layers": [
                 {"type": "image", "src": "A", "start": 0.0, "dur": 2.0, "xin": 0, "xout": 0.5,
                  "x": 0, "y": 0, "w": 120, "h": 120, "opacity": 1, "fit": "cover"},
                 {"type": "image", "src": "B", "start": 1.5, "dur": 1.5, "xin": 0.5, "xout": 0,
                  "x": 0, "y": 0, "w": 120, "h": 120, "opacity": 1, "fit": "cover"},
             ]}
        data, _ = mb.render(e, {"A": self._still("a.jpg", (255, 0, 0)),
                                "B": self._still("b.jpg", (0, 0, 255))})
        out = self._write(data, "o.mp4")
        frame = os.path.join(self.tmp, "mid.png")
        subprocess.run([mb.media_service.resolve_ffmpeg(), "-y", "-v", "error", "-ss", "1.75",
                        "-i", out, "-frames:v", "1", frame], check=True, capture_output=True, timeout=30)
        r, g, b = Image.open(frame).convert("RGB").getpixel((60, 60))
        self.assertGreater(r, 40, (r, g, b))    # the outgoing clip is still visible…
        self.assertGreater(b, 40, (r, g, b))    # …under the incoming one

    def test_no_ramp_means_no_dissolve(self):
        """The same overlap with xin/xout absent must be a hard cut — so a project that never asked for
        a crossfade cannot accidentally get one."""
        e = {"w": 120, "h": 120, "fps": 25, "bg": "#000000", "duration": 3.0,
             "layers": [
                 {"type": "image", "src": "A", "start": 0.0, "dur": 2.0,
                  "x": 0, "y": 0, "w": 120, "h": 120, "opacity": 1, "fit": "cover"},
                 {"type": "image", "src": "B", "start": 1.5, "dur": 1.5,
                  "x": 0, "y": 0, "w": 120, "h": 120, "opacity": 1, "fit": "cover"},
             ]}
        data, _ = mb.render(e, {"A": self._still("a.jpg", (255, 0, 0)),
                                "B": self._still("b.jpg", (0, 0, 255))})
        out = self._write(data, "o.mp4")
        frame = os.path.join(self.tmp, "mid.png")
        subprocess.run([mb.media_service.resolve_ffmpeg(), "-y", "-v", "error", "-ss", "1.75",
                        "-i", out, "-frames:v", "1", frame], check=True, capture_output=True, timeout=30)
        r, g, b = Image.open(frame).convert("RGB").getpixel((60, 60))
        self.assertLess(r, 40, (r, g, b))
        self.assertGreater(b, 180, (r, g, b))


if __name__ == "__main__":
    unittest.main()
