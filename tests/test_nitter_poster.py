"""The Nitter → fediverse poster: seen-state durability and post-card compression.

WHY THESE EXIST

1. Seen-state was persisted ONCE per cycle, after every feed had been processed. A post is public
   the instant it is sent, so a process killed mid-cycle left everything it had just posted still
   marked "new" -- and the next start posted it all over again. A cycle takes about a minute and the
   bot restarts far more often than that (280 starts in one week: deploys, UI toggles, config edits),
   so the on-disk state sat 15 days stale while the bot posted daily. That loop was the flooding, and
   toggling the bot off and on to stop it made it worse. State must now be durable per POST.

2. The card is a screenshot, i.e. PNG, and a card carrying a tweet photo is ~293 KB that way against
   ~59 KB through the app's shared compressor. But a card with no media is flat text, where PNG is
   both the right format and already small (10 KB vs 6.9 KB) -- re-encoding that to JPEG trades 3 KB
   for artifacts on the one thing that has to stay readable. So compression is applied but only KEPT
   when it actually wins, and the resulting content type has to travel: both posters label bare bytes
   "image/png", so a JPEG passed as plain bytes is uploaded under a .png name.
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTS = os.path.join(REPO, "botframework")


def _load_listener():
    """Import nitterListener with botframework/ on the path (it uses root-relative imports)."""
    if BOTS not in sys.path:
        sys.path.insert(0, BOTS)
    spec = importlib.util.spec_from_file_location("_nl", os.path.join(BOTS, "nitterListener.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_nl"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class SeenStateSurvivesAKill(unittest.TestCase):
    def setUp(self):
        self.nl = _load_listener()
        self.dir = tempfile.mkdtemp()
        self.nl._STATE_FILE = os.path.join(self.dir, "seen.json")
        self.nl.NITTER_MAX_POSTS_PER_CYCLE = 3
        self.nl.NITTER_POST_DELAY = 0
        self.items = [{"guid": f"g{i}"} for i in range(6)]
        self.nl._fetch_items = lambda _u: self.items

    def _run_until_killed(self, kill_after):
        """Post items, raising BaseException (not Exception -- that is caught and marked seen) after
        `kill_after` posts, the way a SIGKILL mid-cycle would cut in."""
        posted = []

        def fake_post(_feed, _handle, item):
            posted.append(item["guid"])
            if len(posted) == kill_after:
                raise KeyboardInterrupt("killed mid-cycle")
            return True

        self.nl._post_item = fake_post
        state = {"u": ["g0", "g1", "g2"]}          # g3/g4/g5 are the new ones
        self.nl._save_state(state)
        try:
            self.nl._process_feed({"rss": "u"}, state, lambda: self.nl._save_state(state))
        except KeyboardInterrupt:
            pass
        on_disk = json.load(open(self.nl._STATE_FILE))["u"]
        return posted, [g for g in posted if g not in on_disk]

    def test_a_kill_mid_cycle_does_not_re_post_the_whole_cycle(self):
        posted, would_repost = self._run_until_killed(kill_after=3)
        self.assertEqual(len(posted), 3)
        # Only the post that was in flight at the moment of the kill can repeat; publishing and
        # recording cannot be made atomic. Everything already confirmed must be durable.
        self.assertLessEqual(len(would_repost), 1, f"re-posts {would_repost} of {posted}")

    def test_a_completed_post_is_durable_immediately(self):
        _posted, would_repost = self._run_until_killed(kill_after=2)
        self.assertNotIn("g5", would_repost, "the first confirmed post was not persisted")

    def test_state_is_saved_per_post_not_once_per_cycle(self):
        """A guard on the shape, so the save can't drift back to the end of the loop."""
        src = open(os.path.join(BOTS, "nitterListener.py"), encoding="utf-8").read()
        body = re.search(r"def _process_feed.*?(?=\ndef )", src, re.S).group(0)
        self.assertIn("save()", body, "_process_feed never persists; a restart re-posts the cycle")

    def test_an_unconfirmed_post_stays_unseen(self):
        """The 2026-06-02 rule: a destination blip must retry, not silently drop the item."""
        self.nl._post_item = lambda *_a: False
        state = {"u": ["g0"]}
        self.nl._save_state(state)
        self.nl._process_feed({"rss": "u"}, state, lambda: self.nl._save_state(state))
        self.assertEqual(json.load(open(self.nl._STATE_FILE))["u"], ["g0"])


class CardCompression(unittest.TestCase):
    def test_a_photo_card_is_compressed_and_a_text_card_is_left_alone(self):
        from app.services import media_service
        from PIL import Image
        import io

        def decide(png):
            jpg = media_service.compress_image(png, quality=85)
            return ("image/jpeg", jpg) if len(jpg) <= 0.6 * len(png) else ("image/png", png)

        # Photographic content: smooth tonal variation, which is what a camera actually produces
        # (pure per-pixel noise is JPEG's worst case AND PNG's, so it proves nothing here).
        photo = Image.new("RGB", (900, 600))
        px = photo.load()
        for y in range(600):
            for x in range(900):
                px[x, y] = ((x + y) // 6 % 256, (x * x // 900 + y) // 5 % 256, (y * 2 + x // 3) % 256)
        buf = io.BytesIO()
        photo.save(buf, "PNG")
        self.assertEqual(decide(buf.getvalue())[0], "image/jpeg")

        # Flat text/UI: PNG is the right format and already small, so it must be kept.
        flat = Image.new("RGB", (600, 300), (255, 255, 255))
        px = flat.load()
        for y in range(140, 160):
            for x in range(40, 560):
                px[x, y] = (17, 17, 17)
        buf = io.BytesIO()
        flat.save(buf, "PNG")
        self.assertEqual(decide(buf.getvalue())[0], "image/png")

    def test_the_endpoint_uses_the_shared_compressor(self):
        src = open(os.path.join(REPO, "app", "routers", "media_api.py"), encoding="utf-8").read()
        block = re.search(r"async def render_post_card.*?(?=\n@router|\Z)", src, re.S).group(0)
        self.assertIn("media_service.compress_image", block,
                      "the card must go through the app's compressor, not a bot-local re-encode")
        self.assertIn('"content_type": ct', block,
                      "the real content type must be returned; callers default bare bytes to PNG")


class ContentTypeTravelsToBothDestinations(unittest.TestCase):
    """A compressed card is a JPEG. Both posters label bare bytes "image/png", so the type has to be
    carried as (bytes, mime) or the upload is a JPEG under a .png name."""

    def test_pleroma_upload_honours_a_bytes_mime_tuple(self):
        import types
        captured = {}

        class Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"id": "1"}

        sys.modules["requests"] = types.SimpleNamespace(
            post=lambda *a, **k: (captured.update(k), Resp())[1],
            get=lambda *a, **k: Resp())
        if BOTS not in sys.path:
            sys.path.insert(0, BOTS)
        spec = importlib.util.spec_from_file_location("_pl", os.path.join(BOTS, "pleroma.py"))
        pl = importlib.util.module_from_spec(spec)
        sys.modules["_pl"] = pl
        try:
            spec.loader.exec_module(pl)
        except SystemExit:
            pass

        pl.upload_media_to_pleroma((b"\xff\xd8jpeg", "image/jpeg"))
        name, _fh, mime = captured["files"]["file"]
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(name.endswith(".jpg"), name)

        captured.clear()
        pl.upload_media_to_pleroma(b"\x89PNG")          # legacy bare-bytes path unchanged
        name, _fh, mime = captured["files"]["file"]
        self.assertEqual((name, mime), ("image.png", "image/png"))

    def test_nostr_media_list_honours_a_bytes_mime_tuple(self):
        src = open(os.path.join(BOTS, "nostr.py"), encoding="utf-8").read()
        ns = {}
        exec(compile(re.search(r"def _to_media_list.*?(?=\ndef )", src, re.S).group(0), "x", "exec"), ns)
        self.assertEqual(ns["_to_media_list"](image_bytes=(b"j", "image/jpeg"))[0][1], "image/jpeg")
        self.assertEqual(ns["_to_media_list"](image_bytes=b"p")[0][1], "image/png")


if __name__ == "__main__":
    unittest.main()
