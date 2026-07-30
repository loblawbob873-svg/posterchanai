"""Feed media must reserve its final box BEFORE the image loads — measured in a real browser.

Run: venv-unified/bin/python -m unittest tests.client.test_media_reserve
     (needs google-chrome-stable; skips itself if there is no browser on the node)

The timeline "looked like a jumpy mess as posts come on the screen", and the measurement below is why:
`.media-row img{width:auto;max-height:300px}` reserves NOTHING for an image that has not loaded, so a note
was laid out with a 2px-tall image and then jumped to its real height on decode, shoving everything under
it down. 100-700px of shift, per image, at every breakpoint — worst on a phone, where the short viewport
means nearly every image is loading="lazy" and pops in as you scroll.

The fix is a ratio (--arn) and natural width (--nw) carried inline, plus a per-context cap (--mh), turned
by client.css into `width:min(100%, natural, cap x ratio)`. That is resolvable with no image, so the first
layout pass has the final box.

The assertions are deliberately about MEASURED GEOMETRY rather than about CSS text, because the first
attempt at this passed every string check while reserving 2x2: width/height ATTRIBUTES are presentational
hints, and the author rule `width:auto` beats them. test_attribute_only_form_does_not_reserve pins that
finding so nobody re-derives it.

Two properties are checked for every context x shape x breakpoint:
  reserved == loaded   — the box does not change when the bytes arrive (this is the whole point)
  loaded   ~= today    — the fix did not resize the feed; it only made it stable
"""
import base64
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSS = os.path.join(REPO, "static", "css", "client.css")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")

# (label, wrapper html with {c} where the media goes). Every context in the feed that caps media height.
CONTEXTS = [
    ("media-row single", '<div class="note"><div class="body"><div class="media-row">{c}</div></div></div>'),
    ("media-row pair",   '<div class="note"><div class="body"><div class="media-row">{c}{c}</div></div></div>'),
    ("carousel item",    '<div class="note"><div class="body"><div class="media-car" data-n="2">'
                         '<div class="mc-track"><div class="mc-item">{c}</div></div></div></div></div>'),
    ("inline txt",       '<div class="note"><div class="body"><div class="txt">hello {c}</div></div></div>'),
    ("quoted txt",       '<div class="note"><div class="body"><div class="quoted"><div class="txt">{c}</div></div></div></div>'),
    ("thread-hl row",    '<div class="note thread-hl"><div class="body"><div class="media-row">{c}</div></div></div>'),
]
# Shapes that matter: landscape, portrait (hits the tall cap), panorama (hits neither), and two images
# SMALLER than the cap — the upscale trap, where a naive fix inflates a thumbnail.
SHAPES = {"land": (1600, 1000), "port": (1000, 1600), "wide": (1920, 600),
          "small": (400, 250), "tiny": (120, 120)}
# 390x844 = iPhone 12/13/14 class, the mobile breakpoint (<=820px) that .mobilenav and the media
# overrides key on. 1400x900 is above it, so the desktop caps apply.
VIEWPORTS = [("desktop", 1400, 900), ("mobile", 390, 844)]

# A sub-cap image ends up 2px narrower than it used to: box-sizing is border-box, so an explicit width is
# the border box while the old shrink-to-fit width was content + 2px of border. Invisible, and not worth
# per-context border arithmetic in the calc.
DRIFT_TOLERANCE = 3.0
SHIFT_TOLERANCE = 0.6      # sub-pixel: rounding only


def _png(w, h):
    from PIL import Image
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", (w, h), (60, 80, 140)).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _run_page(html_text, width, height):
    """Render in headless Chrome at a REAL window size and return the <pre id="out"> text.

    The window size has to be real: an earlier version of this harness set the column width in CSS and ran
    at Chrome's default 800px, so the max-width:820px mobile rules were active in the "desktop" pass and
    two carousel results were wrong for a reason that had nothing to do with the code under test.
    """
    tmp = tempfile.mkdtemp(prefix="pcmedia-")
    try:
        path = os.path.join(tmp, "t.html")
        with open(path, "w") as fh:
            fh.write(html_text)
        out = subprocess.run(
            [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
             f"--window-size={width},{height}", "--virtual-time-budget=10000",
             "--dump-dom", "file://" + path],
            capture_output=True, text=True, timeout=180).stdout
        m = re.search(r'<pre id="out">(.*?)</pre>', out, re.S)
        if not m:
            raise AssertionError("harness produced no output; chrome said:\n" + out[-2000:])
        return html.unescape(m.group(1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _build(hint_mode):
    """The test page. `hint_mode` is 'props' (the shipped inline --arn/--nw) or 'attrs' (width/height
    attributes only — the form that silently fails, kept as a regression pin)."""
    with open(CSS) as fh:
        css = fh.read()
    imgs = {k: {"w": w, "h": h, "uri": _png(w, h)} for k, (w, h) in SHAPES.items()}

    def hint(w, h):
        if hint_mode == "attrs":
            return f'width="{w}" height="{h}"'
        return f'width="{w}" height="{h}" style="--arn:{w/h:.6f};--nw:{w}"'

    static, cases = [], []
    for ci, (cname, tpl) in enumerate(CONTEXTS):
        for key, im in imgs.items():
            base_id, cand_id = f"b{ci}_{key}", f"c{ci}_{key}"
            # today = the pre-fix rendering: a plain <img>, no hint at all, fully decoded. The shipped CSS
            # falls back to width:auto for exactly this case, so it still measures the old look.
            static.append(tpl.format(c=f'<img id="{base_id}" src="{im["uri"]}">'))
            static.append(tpl.format(c=f'<img id="{cand_id}" {hint(im["w"], im["h"])} src="{im["uri"]}">'))
            cases.append({"ctx": cname, "ctxIdx": ci, "key": key, "w": im["w"], "h": im["h"],
                          "baseId": base_id, "candId": cand_id, "hint": hint(im["w"], im["h"])})

    # NOTE on fidelity: this page does not reproduce the app's real notes column. That comes from a grid
    # with a sidebar + rightbar and the body{zoom} display scaling, and without them the measured widths
    # are not the widths a user sees (mobile reads ~472px, not 390). It does not matter here, and is not
    # worth rebuilding: all three numbers per case — reserved, today, fixed — are measured in the SAME
    # layout, so the comparisons are exact even though the absolute column is not the app's.
    # What IS authentic is the WINDOW size, because that decides which max-width:820px overrides apply,
    # and the caps are the thing under test. Five shapes x six contexts covers caps that bind and caps
    # that don't at both breakpoints.
    return """<!doctype html><html><head><meta charset="utf-8"><style>
%CSS%
#pane{width:min(600px, 100vw)}
</style></head><body>
<div class="app"><div class="main"><div class="feed"><div id="pane">%STATIC%<div id="scratch"></div></div></div></div></div>
<pre id="out">PENDING</pre>
<script>
const CASES=%CASES%, TPL=%TPL%;
function box(el){const r=el.getBoundingClientRect();return [Math.round(r.width*100)/100,Math.round(r.height*100)/100];}
function go(){
  const lines=[]; const host=document.getElementById('scratch');
  for(const c of CASES){
    // RESERVED: the hint is present, there is no src, nothing has loaded. This is the state the browser
    // lays the card out in the moment it is inserted.
    host.innerHTML = TPL[c.ctxIdx].split('{c}').join('<img '+c.hint+'>');
    const R = box(host.querySelector('img'));
    host.innerHTML = '';
    const B = box(document.getElementById(c.baseId));   // today's loaded size
    const S = box(document.getElementById(c.candId));   // the fix's loaded size
    lines.push(JSON.stringify({ctx:c.ctx,key:c.key,w:c.w,h:c.h,reserved:R,today:B,fixed:S}));
  }
  document.getElementById('out').textContent=lines.join('\\n');
}
if(document.readyState==='complete') go(); else window.addEventListener('load', go);
</script></body></html>""".replace("%CSS%", css).replace("%STATIC%", "\n".join(static)) \
       .replace("%CASES%", json.dumps(cases)).replace("%TPL%", json.dumps([t for _, t in CONTEXTS]))


@unittest.skipIf(not CHROME, "no chrome on this node")
class TestMediaReservation(unittest.TestCase):
    _cache = {}

    def rows(self, hint_mode, vw, vh):
        key = (hint_mode, vw)
        if key not in self._cache:
            txt = _run_page(_build(hint_mode), vw, vh)
            self._cache[key] = [json.loads(l) for l in txt.strip().splitlines() if l.startswith("{")]
        return self._cache[key]

    def test_reserved_box_equals_loaded_box(self):
        """The box does not change when the image arrives — nothing below it ever moves."""
        for label, vw, vh in VIEWPORTS:
            for r in self.rows("props", vw, vh):
                shift = abs(r["reserved"][1] - r["fixed"][1])
                self.assertLess(shift, SHIFT_TOLERANCE,
                                f"{label} {r['ctx']} {r['key']} {r['w']}x{r['h']}: reserved "
                                f"{r['reserved']} then rendered {r['fixed']} — {shift:.1f}px of shift")

    def test_reserved_box_is_not_empty(self):
        """Guards the failure mode that started this: a 'reservation' of 2x2 passes a shift check
        trivially if the loaded box is also tiny, so assert real height was actually held."""
        for label, vw, vh in VIEWPORTS:
            for r in self.rows("props", vw, vh):
                self.assertGreater(r["reserved"][1], 10,
                                   f"{label} {r['ctx']} {r['key']}: reserved nothing ({r['reserved']})")

    def test_the_feed_still_looks_the_same(self):
        """Stability was not bought with a resize: every shape renders the size it renders today."""
        for label, vw, vh in VIEWPORTS:
            for r in self.rows("props", vw, vh):
                dw = abs(r["fixed"][0] - r["today"][0])
                dh = abs(r["fixed"][1] - r["today"][1])
                self.assertLess(max(dw, dh), DRIFT_TOLERANCE,
                                f"{label} {r['ctx']} {r['key']} {r['w']}x{r['h']}: was {r['today']}, "
                                f"now {r['fixed']} — the fix changed the layout, not just its timing")

    def test_reserved_box_keeps_the_images_aspect_ratio(self):
        """The reserved box must have the IMAGE's shape, not a clamped one.

        This is the invariant that catches the failure I was most worried about while writing the CSS: a
        context whose height cap has no matching --mh reserves a width from the wrong cap, `max-height`
        then clamps the height on its own, and the box ends up the wrong shape — which shows as a stretch
        (or, with object-fit, a letterbox) plus a shift. It found exactly that in the rightbar's
        notification thumbnails, which inherit --mh:220px from .notif-ctx but cap at 74px.

        Unlike the absolute widths, a ratio is immune to the harness not reproducing the real column or
        the desktop body{zoom} — which is why the assertion is written this way round.
        """
        for label, vw, vh in VIEWPORTS:
            for r in self.rows("props", vw, vh):
                want = r["w"] / r["h"]
                got = r["reserved"][0] / max(0.01, r["reserved"][1])
                self.assertAlmostEqual(
                    got, want, delta=0.02,
                    msg=f"{label} {r['ctx']} {r['key']} {r['w']}x{r['h']}: reserved {r['reserved']} "
                        f"is {got:.3f}:1 but the image is {want:.3f}:1 — a cap clamped one axis alone, "
                        f"so that context is probably missing its --mh")

    def test_attribute_only_form_does_not_reserve(self):
        """width/height attributes alone reserve NOTHING, and this is the pin for that.

        They are presentational hints, so the author rule `width:auto` beats them. It is the obvious fix,
        it is what every 'add width and height to your images' article says, and here it does nothing —
        which is invisible unless you measure, because the markup looks correct.
        """
        rows = self.rows("attrs", *VIEWPORTS[0][1:])
        reserved = [r for r in rows if r["reserved"][1] > 10]
        self.assertEqual(reserved, [], "attributes now reserve space — if a CSS change made width "
                                       "resolvable, the inline --arn/--nw hint may be redundant")


if __name__ == "__main__":
    unittest.main()
