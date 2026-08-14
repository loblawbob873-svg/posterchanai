"""VideoMount, measured in a real browser: only what you can see may hold a media player.

Run: venv-unified/bin/python -m unittest tests.client.test_video_mount_browser
     (needs google-chrome-stable; skips itself if there is no browser on the node)

tests/client/test_video_lazy_mount.py pins the SHAPE of the code (data-vsrc, no eager src, a cap, a
release, both observer branches). This one runs the actual module out of app.js and measures what it
does to real elements:

  offscreen stays cold   — a video below the fold never gets a src, so never a decoder
  detach releases        — a node dropped by a re-render has its src cleared THERE AND THEN; waiting for
                           GC is what let a rebuild burst pile up players until the renderer died
  a move is not a detach — insertBefore reports remove-then-add, and treating that as a removal would
                           restart every visible video on every timeline reconcile

The module is extracted from app.js rather than copied, so this cannot drift from what ships.

NOT covered here, deliberately: the scroll-driven swap. Under `--dump-dom` an IntersectionObserver
delivers its FIRST callback and then never recomputes — measured, not assumed: a plain page whose
container is scrolled reports the new scrollTop and the old intersection state, and so does one whose
leading elements are removed. Testing it would take a real CDP session; the branch itself
(`isIntersecting ? mount : unmount`) is asserted in test_video_lazy_mount.py instead.
"""
import html
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "static", "js", "client", "app.js")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")

VID_H = 400          # each clip's box; #host below is 600 tall and the observer margin is 300
N_VIDS = 6


def _extract(src, opener_text):
    """The `const X = (function(){ … })();` run beginning at `opener_text`.

    Bounded by the module's own closing line rather than by paren counting: the block is full of prose
    comments, and one unbalanced parenthesis in a sentence would silently truncate what we test.
    """
    i = src.index(opener_text)
    end = src.index("\n  })();\n", i)
    return src[i : end + len("\n  })();")]


def _page(module_js):
    vids = "".join(
        f'<div class="wrap" id="w{i}">'
        f'<video id="v{i}" data-vsrc="clip{i}.mp4" controls preload="none" playsinline '
        f'style="display:block;width:300px;height:{VID_H}px;background:#222"></video></div>'
        for i in range(N_VIDS)
    )
    return f"""<!doctype html><meta charset="utf-8">
<style>body{{margin:0}} .wrap{{margin:0}}
/* A real scroll CONTAINER, not the window: that is the app's shape (#feed / #rb-list scroll, the page
   does not), and headless Chrome's window scroll is unreliable under --dump-dom. */
#host{{height:600px;overflow:auto}}</style>
<div id="host">{vids}</div>
<pre id="out"></pre>
<script>
{module_js}
const out = [];
const state = () => [...document.querySelectorAll('video[data-vsrc]')]
  .map(v => v.getAttribute('src') ? 1 : 0).join('');
function step(fn, ms){{ return new Promise(r => setTimeout(() => {{ fn(); r(); }}, ms)); }}
(async () => {{
  await step(()=>{{ out.push(['initial', state()]); }}, 400);
  // Detach: what a re-render does to a MOUNTED video.
  const doomed = document.querySelector('video[data-vsrc][src]');
  const doomedId = doomed ? doomed.id : 'none';
  await step(()=>{{ if(doomed) doomed.parentNode.remove(); }}, 50);
  await step(()=>{{ out.push(['detached', doomedId, doomed ? (doomed.getAttribute('src')||'') : 'x']); }}, 400);
  // Move: remove-then-add in one go, which a MutationObserver reports exactly like a removal.
  const moved = document.querySelector('video[data-vsrc][src]');
  const movedId = moved ? moved.id : 'none';
  await step(()=>{{ if(moved) document.getElementById('host').appendChild(moved.parentNode); }}, 50);
  await step(()=>{{ out.push(['moved', movedId, moved ? (moved.getAttribute('src')||'') : 'x']); }}, 400);
  document.getElementById('out').textContent = JSON.stringify(out);
}})();
</script>"""


def _run(page, width=800, height=600):
    tmp = tempfile.mkdtemp(prefix="pcvmount-")
    try:
        path = os.path.join(tmp, "t.html")
        with open(path, "w") as fh:
            fh.write(page)
        res = subprocess.run(
            [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
             f"--window-size={width},{height}", "--virtual-time-budget=15000",
             "--dump-dom", "file://" + path],
            capture_output=True, text=True, timeout=180).stdout
        m = re.search(r'<pre id="out">(.*?)</pre>', res, re.S)
        if not m or not m.group(1).strip():
            raise AssertionError("harness produced no output; chrome said:\n" + res[-2000:])
        import json
        return json.loads(html.unescape(m.group(1)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(CHROME, "no chrome on this node")
class VideoMountBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """One chrome run feeds all three tests, so a run that measured NOTHING must not be reported
        as three separate failures about video mounting.

        THE FLAKE THIS FIXES, and it is a harness fault rather than a product one. Every assertion
        below reads `cls.rows`, so when the page comes back with nothing mounted at all — `initial`
        all zeros, both node ids "none" — all three fail at once with messages that describe a broken
        lazy-mount ("the video in view must mount", "nothing was mounted to detach"). That is exactly
        what a real regression looks like, which is what made it expensive: it appeared only in a full
        `pytest tests/` run and never in isolation or in `tests/client/` alone, so it read as "the
        suite caught something the subset did not". It did not. Under whole-suite load chrome
        occasionally spends the entire `--virtual-time-budget` starting up, and the IntersectionObserver's
        first callback never lands before `--dump-dom` prints the page.

        So a degenerate run is retried rather than believed. This does NOT weaken the test: if lazy
        mounting is genuinely broken, every attempt is degenerate and it fails loudly below, with the
        `initial` string from the last attempt to say so. `got.count("1") < N_VIDS` still catches the
        opposite failure, where everything mounts eagerly.
        """
        with open(APP) as fh:
            src = fh.read()
        page = _page(_extract(src, "const VideoMount = (function()"))
        last = None
        for _ in range(3):
            rows = {r[0]: r[1:] for r in _run(page)}
            last = rows
            # "Did the observer run at all?" — the one question that separates a measurement from a
            # browser that never got there. Any '1' means it did, and the assertions take over.
            if "1" in rows.get("initial", [""])[0]:
                break
        cls.rows = last

    def test_offscreen_videos_never_get_a_src(self):
        got = self.rows["initial"][0]
        self.assertEqual(got[0], "1", f"the video in view must mount (got {got})")
        self.assertEqual(got[-1], "0", f"the video far below the fold must stay cold (got {got})")
        self.assertLess(got.count("1"), N_VIDS, f"everything mounted — lazy mounting is not running ({got})")

    def test_detaching_a_node_releases_its_decoder(self):
        vid, src = self.rows["detached"]
        self.assertNotEqual(vid, "none", "nothing was mounted to detach — harness problem")
        self.assertEqual(src, "", f"{vid} kept its src after being dropped from the DOM")

    def test_moving_a_node_is_not_a_detach(self):
        vid, src = self.rows["moved"]
        self.assertNotEqual(vid, "none", "nothing was mounted to move — harness problem")
        self.assertTrue(src, f"{vid} lost its src on a MOVE; a reconcile would restart every video")


if __name__ == "__main__":
    unittest.main()
