"""VideoMount's first-frame grace — why video previews were black boxes over Tor.

A timeline video carries no src until VideoMount attaches one, so its preview IS a network fetch:
metadata (the moov atom, at the END of a non-faststart MP4) plus a seek to #t=0.1. Measured on real
timeline videos in headless Chrome, direct versus through a tor SOCKS proxy:

    poastcdn     painted @319ms   →  @2347ms
    nicecrew     painted @356ms   →  @5135ms
    libernet     painted @1099ms  →  @38569ms   (stalled at 3.2s, metadata only at 36s)

and unmounting mid-fetch keeps NOTHING: aborting at 1.5s left readyState back at 0, and the remount
paid the full 5367ms again. So with an unmount-on-leave, scrolling always beat the fetch and no video
ever painted — on the connection where the user can least tell whether it is them, tor, or us.

The fix is that a load which has not produced a frame yet is not idle, and is left alone for a while.
What must NOT change is the reason this module exists (the Android WebView dying when too many
decoders are live), so the two forced paths are asserted just as hard as the grace itself:

  cap         MAX_MOUNTED is a hard limit on live decoders, not a preference. A still-loading video
              may not refuse there, or it would starve the one the user is looking at.
  detach      A node dropped by a re-render frees its decoder immediately, grace or no grace.

IntersectionObserver is STUBBED here (and only here). The real one does not recompute under
--dump-dom — measured, and the reason test_video_mount_browser.py deliberately tests no scrolling —
so intersection changes are driven by hand, which is also the only way to test "it left the viewport"
without a scroll.
"""
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from .test_video_mount_browser import _extract, APP, CHROME

N_VIDS = 12          # more than MAX_MOUNTED (8), so the cap has to evict


def _page(module_js):
    vids = "".join(
        f'<video id="v{i}" data-vsrc="never-loads-{i}.mp4" controls preload="none"></video>'
        for i in range(N_VIDS)
    )
    # The stub records every observed element and hands the callback to the page, so the test can say
    # "this one left the viewport" precisely. Defined BEFORE the module, which captures it at build.
    return f"""<!doctype html><meta charset="utf-8">
<script>
window.__io = null;
window.IntersectionObserver = class {{
  constructor(cb){{ this.cb = cb; this.seen = []; window.__io = this; }}
  observe(el){{ this.seen.push(el); }}
  unobserve(el){{ this.seen = this.seen.filter(x => x !== el); }}
  disconnect(){{ this.seen = []; }}
  fire(els, on){{ this.cb(els.map(t => ({{ target: t, isIntersecting: on }}))); }}
}};
</script>
<div id="host">{vids}</div>
<pre id="out"></pre>
<script>
{module_js}
const out = [];
const V = i => document.getElementById('v' + i);
const hasSrc = i => V(i) && V(i).getAttribute('src') ? 1 : 0;
const wait = ms => new Promise(r => setTimeout(r, ms));
(async () => {{
  const io = window.__io;
  await wait(100);
  // Everything comes into view at once. None of these can ever paint (the URLs do not resolve), which
  // is what a slow link looks like from here: mounted, fetching, no frame yet.
  io.fire(io.seen.slice(), true);
  await wait(300);
  const mountedCount = [...Array(N_VIDS).keys()].filter(hasSrc).length;
  out.push(['cap', mountedCount]);

  // v0 leaves the viewport while still loading. Its fetch must survive.
  const before = hasSrc(0);
  io.fire([V(0)], false);
  await wait(300);
  out.push(['left_while_loading', before, hasSrc(0)]);

  // A mounted, still-loading video dropped from the DOM frees its decoder anyway.
  const doomed = [...Array(N_VIDS).keys()].find(i => hasSrc(i));
  const el = V(doomed);
  el.remove();
  await wait(300);
  out.push(['detached', doomed, el.getAttribute('src') || '']);

  document.getElementById('out').textContent = JSON.stringify(out);
}})();
</script>""".replace("N_VIDS", str(N_VIDS))


def _run(page):
    tmp = tempfile.mkdtemp(prefix="pcvgrace-")
    try:
        path = os.path.join(tmp, "t.html")
        with open(path, "w") as fh:
            fh.write(page)
        res = subprocess.run(
            [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
             "--window-size=800,600", "--virtual-time-budget=15000", "--dump-dom", "file://" + path],
            capture_output=True, text=True, timeout=180).stdout
        m = re.search(r'<pre id="out">(.*?)</pre>', res, re.S)
        if not m or not m.group(1).strip():
            raise AssertionError("harness produced no output; chrome said:\n" + res[-2000:])
        return json.loads(html.unescape(m.group(1)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(CHROME, "no chrome on this node")
class FirstFrameGrace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(APP) as fh:
            src = fh.read()
        cls.rows = {r[0]: r[1:] for r in _run(_page(_extract(src, "const VideoMount = (function()")))}

    def test_a_loading_video_survives_leaving_the_viewport(self):
        """THE regression. Releasing here aborts the fetch and keeps nothing, so over tor — where the
        first frame costs seconds, not milliseconds — scrolling beats every preview, forever."""
        before, after = self.rows["left_while_loading"]
        self.assertEqual(before, 1, "nothing was mounted to test — harness problem")
        self.assertEqual(after, 1,
                         "a video still fetching its first frame was released when it scrolled away; "
                         "on a slow link that fetch can never finish and the preview never appears")

    def test_the_decoder_cap_still_holds(self):
        """The grace must not become a way to keep 12 media players alive. This is the Android WebView
        render-process death this whole module exists to prevent."""
        (n,) = self.rows["cap"]
        self.assertLessEqual(n, 8, f"{n} videos hold a decoder at once; MAX_MOUNTED is 8")
        self.assertGreater(n, 0, "nothing mounted at all — lazy mounting is broken")

    def test_detaching_still_frees_immediately(self):
        """A re-render dropping the node is not 'it scrolled away' — there is nothing left to paint
        into, so the grace must not apply."""
        vid, src = self.rows["detached"]
        self.assertEqual(src, "", f"v{vid} kept its src after being dropped from the DOM")
