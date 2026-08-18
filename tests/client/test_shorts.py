"""Discover → Shorts: NIP-71 short videos, in the shape Divine ACTUALLY publishes.

Measured off wss://relay.divine.video (2026-08-18): kind 34236, imeta with `url`/`m video/mp4`/
`image` poster/`dim 1080x1920`, plus title/duration tags. The parser is LIFTED from app.js and RUN
against a real captured event, and the renderer's decoder discipline (one <video> mounted at a
time) is pinned at source — a <video src> IS a decoder, and 60 of them is how a feed kills a
WebView."""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
NODE = shutil.which("node") or shutil.which("nodejs")

DIVINE_EV = {
    "kind": 34236, "pubkey": "ab" * 32, "id": "cd" * 32, "created_at": 1787078387, "content": "#relatable",
    "tags": [
        ["d", "0a0a3b40"],
        ["imeta",
         "url https://media.divine.video/0a0a3b40",
         "m video/mp4",
         "image https://media.divine.video/poster1",
         "dim 1080x1920", "size 6321304"],
        ["title", "Anyone else have this problem"],
        ["duration", "6"],
    ],
}


def _lift(name):
    m = re.search(r"\n  (?:function %s|const %s)" % (re.escape(name), re.escape(name)), APP)
    assert m, name + " moved in app.js"
    start = m.start() + 1
    i = APP.index("{", m.end() - 1)
    d = 0
    while i < len(APP):
        if APP[i] == "{": d += 1
        elif APP[i] == "}":
            d -= 1
            if not d: break
        i += 1
    return APP[start:i + 1]


@unittest.skipIf(not NODE, "no node on this node")
class ParserTests(unittest.TestCase):
    def _run(self, ev):
        js = "%s\nprocess.stdout.write(JSON.stringify(_shortOf(%s)));" % (_lift("_shortOf"), json.dumps(ev))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-1000:])
        return json.loads(r.stdout)

    def test_a_real_divine_event_parses(self):
        s = self._run(DIVINE_EV)
        self.assertEqual(s["url"], "https://media.divine.video/0a0a3b40")
        self.assertEqual(s["poster"], "https://media.divine.video/poster1")
        self.assertEqual(s["title"], "Anyone else have this problem")
        self.assertEqual(s["dur"], 6)

    def test_no_url_is_no_card_never_a_throw(self):
        ev = dict(DIVINE_EV, tags=[["d", "x"], ["title", "no media"]], content="just words")
        self.assertIsNone(self._run(ev))

    def test_a_non_http_url_is_refused(self):
        ev = dict(DIVINE_EV)
        ev = json.loads(json.dumps(ev))
        ev["tags"][1] = ["imeta", "url javascript:alert(1)", "m video/mp4"]
        self.assertIsNone(self._run(ev))

    def test_a_non_http_poster_is_dropped_not_rendered(self):
        ev = json.loads(json.dumps(DIVINE_EV))
        ev["tags"][1] = ["imeta", "url https://ok.example/v.mp4", "image javascript:alert(1)"]
        self.assertEqual(self._run(ev)["poster"], "")

    def test_a_bare_mp4_link_in_content_still_renders(self):
        ev = dict(DIVINE_EV, kind=22, tags=[["d", "y"]],
                  content="look https://cdn.example.com/clip.mp4 !")
        s = self._run(ev)
        self.assertEqual(s["url"], "https://cdn.example.com/clip.mp4")


class RendererDiscipline(unittest.TestCase):
    def _seg(self):
        a = APP.index("function _shortsPlayer(")
        return APP[a:a + 5200]

    def test_videos_mount_lazily_and_unmount_off_screen(self):
        seg = self._seg()
        self.assertIn("IntersectionObserver", seg)
        self.assertIn("removeAttribute('src')", seg, "an off-screen card keeps its decoder")
        tpl = seg[seg.index("host.innerHTML ="):seg.index("decorateProfiles()")]
        self.assertNotIn("<video", tpl,
                         "videos are in the initial HTML — 60 decoders on first paint")

    def test_muted_autoplay_and_a_way_to_unmute(self):
        seg = self._seg()
        self.assertIn("_shortsMuted", seg)
        self.assertIn("playsInline", seg)

    def test_addressable_dedup_and_the_kinds(self):
        a = APP.index("async function renderShorts()")
        seg = APP[a:a + 2600]
        self.assertIn("kinds:[34236,22]", seg.replace('"', "'"))
        self.assertIn("e.kind+':'+e.pubkey+':'", seg, "no coordinate dedup — an edited short doubles")

    def test_the_grid_is_the_front_door(self):
        """"only 1 video at a time? bad UI" — browsing shows MANY (poster tiles, duration badges);
        the full-screen player is where a tap lands, starting at that short."""
        a = APP.index("async function renderShorts()")
        seg = APP[a:a + 2600]
        self.assertIn("_shortsGrid(host)", seg)
        g = APP.index("function _shortsGrid(")
        gseg = APP[g:g + 1800]
        self.assertIn("short-tile", gseg)
        self.assertIn("short-dur", gseg, "no duration badge on the tiles")
        self.assertIn("_shortsPlayer(host, _shortsAt)", gseg)

    def test_the_view_is_wired_into_nav_sheet_and_dispatch(self):
        self.assertIn("if (VIEW==='shorts') return renderShorts();", APP)
        self.assertIn("['shorts','tv','Shorts']", APP)
        html = open(os.path.join(ROOT, "templates", "client.html"), encoding="utf-8").read()
        self.assertIn('data-view="shorts"', html)


if __name__ == "__main__":
    unittest.main()


class PostingTests(unittest.TestCase):
    """Posting writes Divine's measured shape, and the upload files under the drive's Posts folder
    like every composer attachment — the builder is LIFTED and RUN."""

    def test_the_builder_emits_divines_shape(self):
        js = "%s\nprocess.stdout.write(JSON.stringify(_shortTagsFor({url:'https://x/aa', sha:'f'.repeat(64)}, {mime:'video/mp4', size:123, title:'hi', poster:'https://x/p.jpg', dim:'1080x1920', dur:5.4})));" % _lift("_shortTagsFor")
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        tags = json.loads(r.stdout)
        d = dict((t[0], t[1:]) for t in tags)
        self.assertEqual(d["d"][0], "f" * 64)
        im = d["imeta"]
        self.assertIn("url https://x/aa", im)
        self.assertIn("m video/mp4", im)
        self.assertIn("image https://x/p.jpg", im)
        self.assertIn("dim 1080x1920", im)
        self.assertIn("x " + "f" * 64, im)
        self.assertEqual(d["duration"][0], "5")
        self.assertEqual(d["title"][0], "hi")
        # …and our own reader must parse what our own writer produces (round trip)
        js2 = "%s\n%s\nconst tags=_shortTagsFor({url:'https://x/aa', sha:'f'.repeat(64)},{mime:'video/mp4',title:'hi',poster:'https://x/p.jpg',dur:5});process.stdout.write(JSON.stringify(_shortOf({kind:34236,content:'hi',tags})));" % (_lift("_shortTagsFor"), _lift("_shortOf"))
        r2 = subprocess.run([NODE, "-e", js2], capture_output=True, text=True, timeout=30)
        self.assertEqual(r2.returncode, 0, r2.stderr[-800:])
        rt = json.loads(r2.stdout)
        self.assertEqual(rt["url"], "https://x/aa")
        self.assertEqual(rt["poster"], "https://x/p.jpg")

    def test_uploads_file_under_the_posts_folder(self):
        a = APP.index("async function _postShort(")
        seg = APP[a:a + 3600]
        self.assertEqual(seg.count("{folder:'Posts'}"), 2,
                         "the video or its poster lands in Files as a bare sha instead of Posts")

    def test_the_grid_offers_the_post_button(self):
        g = APP.index("function _shortsGrid(")
        seg = APP[g:g + 2600]
        self.assertIn("short-post", seg)
        self.assertIn('accept="video/*"', seg)

    def test_no_phantom_bindings_in_the_post_path(self):
        """Shipped broken once: a stray `_aiHold` (a name from another module) threw ReferenceError
        on the first click of Post — reported as "action failed when uploading". Every name the
        function reaches for must exist in app.js."""
        a = APP.index("async function _postShort(")
        seg = APP[a:a + 3600]
        self.assertNotIn("_aiHold", seg)
        import re as _re
        for name in set(_re.findall(r"(?<![.\w])(_[a-zA-Z]\w+)\s*\(", seg)):
            self.assertTrue(("function " + name) in APP or (name + " =") in APP
                            or (name + "=") in APP,
                            "%s is called in _postShort but defined nowhere in app.js" % name)
