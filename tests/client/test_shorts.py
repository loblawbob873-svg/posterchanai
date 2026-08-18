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
        a = APP.index("async function renderShorts()")
        return APP[a:a + 5200]

    def test_videos_mount_lazily_and_unmount_off_screen(self):
        seg = self._seg()
        self.assertIn("IntersectionObserver", seg)
        self.assertIn("removeAttribute('src')", seg, "an off-screen card keeps its decoder")
        tpl = seg[seg.index("wrap.innerHTML = vids.map"):seg.index("decorateProfiles()")]
        self.assertNotIn("<video", tpl,
                         "videos are in the initial HTML — 60 decoders on first paint")

    def test_muted_autoplay_and_a_way_to_unmute(self):
        seg = self._seg()
        self.assertIn("_shortsMuted", seg)
        self.assertIn("playsInline", seg)

    def test_addressable_dedup_and_the_kinds(self):
        seg = self._seg()
        self.assertIn("kinds:[34236,22]", seg.replace('"', "'"))
        self.assertIn("e.kind+':'+e.pubkey+':'", seg, "no coordinate dedup — an edited short doubles")

    def test_the_view_is_wired_into_nav_sheet_and_dispatch(self):
        self.assertIn("if (VIEW==='shorts') return renderShorts();", APP)
        self.assertIn("['shorts','tv','Shorts']", APP)
        html = open(os.path.join(ROOT, "templates", "client.html"), encoding="utf-8").read()
        self.assertIn('data-view="shorts"', html)


if __name__ == "__main__":
    unittest.main()
