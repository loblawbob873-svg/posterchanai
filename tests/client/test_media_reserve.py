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

Checked for every context x shape x breakpoint:
  reserved == loaded   — the box does not change when the bytes arrive (this is the whole point)
  reserved ratio       — it has the media's own shape, so no cap clamped one axis alone
  loaded   ~= today    — the fix did not resize the feed; it only made it stable

Notifications and VIDEO were added after the fact, and both earned their place. The first version of this
file emitted an <img> for every case and had no .notif-ctx context, which let two real bugs ship:
  - `.notif img{width:34px;height:34px;border-radius:50%}`, meant for the avatar, is specificity (0,1,1) —
    a tie with `.media-row img`, declared later, so it won. Every photo in a notification preview was a
    34px circle. Video was never matched by it, which is why the report was "videos are jumpy in
    notifications" and not "images".
  - `.notif-ctx`'s 220px cap lost its own tie to `.media-row img:only-child{--mh:720px}`, so a lone video
    in a notification row reserved 400x640 on a 390px phone.
Neither was visible in CSS review; both are obvious the moment something measures a box.
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
FFMPEG = shutil.which("ffmpeg")
# The provisional shape _dimAttrs falls back to when a post carries no imeta dim. Kept in step with
# app.js _DIM_GUESS: several assertions below are about what a WRONG guess costs.
GUESS_W, GUESS_H = 1600, 1000

# (label, wrapper html with {c} where the media goes). Every context in the feed that caps media height.
CONTEXTS = [
    ("media-row single", '<div class="note"><div class="body"><div class="media-row">{c}</div></div></div>'),
    ("media-row pair",   '<div class="note"><div class="body"><div class="media-row">{c}{c}</div></div></div>'),
    ("carousel item",    '<div class="note"><div class="body"><div class="media-car" data-n="2">'
                         '<div class="mc-track"><div class="mc-item">{c}</div></div></div></div></div>'),
    ("inline txt",       '<div class="note"><div class="body"><div class="txt">hello {c}</div></div></div>'),
    ("quoted txt",       '<div class="note"><div class="body"><div class="quoted"><div class="txt">{c}</div></div></div></div>'),
    ("thread-hl row",    '<div class="note thread-hl"><div class="body"><div class="media-row">{c}</div></div></div>'),
    # Notifications. A notification's context preview is quotedDiv(), so its media lands in
    # .notif-ctx > .quoted > .media-row (one attachment) or .mc-item (2+). These were missing from the
    # first version of this file, and that is how a real bug shipped: the .notif-ctx cap lost a
    # SPECIFICITY TIE to .media-row img:only-child{--mh:720px}, so a lone portrait video in a
    # notification row reserved 400x640 on a 390px phone. See test_notification_media_stays_a_row.
    # `.notif` is a FLEX container and notifHtml puts the context inside an unnamed flex item next to the
    # icon and avatar — reproduced here because a shrink-to-fit flex item with no text gives `min(100%,…)`
    # nothing to resolve against and the reserved box collapses. The app always has that text.
    ("notif row",        '<div class="notif like"><span class="ic">&#9829;</span>'
                         '<img class="notif-av"><div><b>someone</b> reacted to your post'
                         '<div class="notif-ctx"><div class="quoted"><div class="media-row">{c}</div>'
                         '</div></div><div class="muted small">2m</div></div></div>'),
    ("notif carousel",   '<div class="notif like"><span class="ic">&#9829;</span>'
                         '<img class="notif-av"><div><b>someone</b> reacted to your post'
                         '<div class="notif-ctx"><div class="quoted"><div class="media-car" data-n="2">'
                         '<div class="mc-track"><div class="mc-item">{c}</div></div></div>'
                         '</div></div><div class="muted small">2m</div></div></div>'),
]
# The height a notification's context media may never exceed — it is a ROW, not a feed card. Desktop cap
# is 220px and mobile 170px; a carousel inside .quoted legitimately takes the 240px quoted cap, so the
# assertion allows the largest of them plus the card's own chrome.
NOTIF_MAX_MEDIA_H = 260
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


_MP4_CACHE = {}


def _mp4(w, h):
    """A one-frame MP4 of exactly w x h, as a data URI. Videos need real geometry coverage of their own:
    they are the case this file originally MISSED (every case emitted an <img>), and a <video> gets its
    intrinsic size from `loadedmetadata` rather than a `load` event, so it is a genuinely different path.

    Memoised: the page is built once per (hint mode, viewport), so without this the same two clips are
    re-encoded six times per run."""
    if not FFMPEG:
        return None
    if (w, h) in _MP4_CACHE:
        return _MP4_CACHE[(w, h)]
    tmp = tempfile.mkdtemp(prefix="pcmedia-v")
    try:
        out = os.path.join(tmp, "v.mp4")
        p = subprocess.run([FFMPEG, "-hide_banner", "-y", "-f", "lavfi",
                            "-i", f"color=c=blue:s={w}x{h}:d=1:r=10", "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
                           capture_output=True, timeout=120)
        if p.returncode != 0 or not os.path.exists(out):
            return None
        with open(out, "rb") as fh:
            _MP4_CACHE[(w, h)] = "data:video/mp4;base64," + base64.b64encode(fh.read()).decode()
        return _MP4_CACHE[(w, h)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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

    # Videos too — two shapes is enough, since the question for them is whether the same mechanism works
    # on an element whose intrinsic size arrives via loadedmetadata. Skipped entirely without ffmpeg.
    vids = {}
    for key, (w, h) in {"vland": (640, 400), "vport": (400, 640)}.items():
        uri = _mp4(w, h)
        if uri:
            vids[key] = {"w": w, "h": h, "uri": uri}

    static, cases = [], []
    for ci, (cname, tpl) in enumerate(CONTEXTS):
        for kind, items in (("img", imgs), ("video", vids)):
            for key, im in items.items():
                base_id, cand_id = f"b{ci}_{key}", f"c{ci}_{key}"
                if kind == "img":
                    base = f'<img id="{base_id}" src="{im["uri"]}">'
                    cand = f'<img id="{cand_id}" {hint(im["w"], im["h"])} src="{im["uri"]}">'
                else:
                    # #t=0.1 mirrors what _media() appends to make the browser paint a poster frame.
                    tail = 'controls preload="metadata" playsinline'
                    base = f'<video id="{base_id}" src="{im["uri"]}#t=0.1" {tail}></video>'
                    cand = f'<video id="{cand_id}" {hint(im["w"], im["h"])} src="{im["uri"]}#t=0.1" {tail}></video>'
                # base = the pre-fix rendering: no hint at all, fully loaded. The shipped CSS falls back to
                # width:auto for exactly this case, so it still measures the old look.
                static.append(tpl.format(c=base))
                static.append(tpl.format(c=cand))
                cases.append({"ctx": cname, "ctxIdx": ci, "key": key, "kind": kind,
                              "w": im["w"], "h": im["h"], "baseId": base_id, "candId": cand_id,
                              "hint": hint(im["w"], im["h"]),
                              "guessHint": hint(GUESS_W, GUESS_H)})

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
    const tag = c.kind==='video' ? 'video' : 'img';
    // RESERVED: the hint is present, there is no src, nothing has loaded. This is the state the browser
    // lays the card out in the moment it is inserted.
    // id, not querySelector(tag): the notification templates carry a .notif-av avatar BEFORE {c}, so
    // querySelector('img') measured the avatar and reported a 24x24 "collapsed reservation".
    const open = (h)=> '<'+tag+' id="__probe" '+h+'>' + (tag==='video' ? '</video>' : '');
    host.innerHTML = TPL[c.ctxIdx].split('{c}').join(open(c.hint));
    const R = box(host.querySelector('#__probe'));
    // GUESS-RESERVED: the same slot, but sized from the 16:10 fallback instead of the real shape — i.e.
    // a post whose imeta carries no dim, on first sight. Where the context's cap BINDS, this has the
    // same HEIGHT as R, which is what makes a wrong guess cost nothing vertically.
    host.innerHTML = TPL[c.ctxIdx].split('{c}').join(open(c.guessHint));
    const G = box(host.querySelector('#__probe'));
    host.innerHTML = '';
    const B = box(document.getElementById(c.baseId));   // today's loaded size
    const S = box(document.getElementById(c.candId));   // the fix's loaded size
    lines.push(JSON.stringify({ctx:c.ctx,key:c.key,kind:c.kind,w:c.w,h:c.h,
                               reserved:R,guessReserved:G,today:B,fixed:S}));
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

    def test_notification_media_stays_a_row(self):
        """A notification's context preview must stay ROW-sized, on desktop and on mobile.

        This is the regression pin for a real bug. `.notif-ctx .media-row video` caps at 220px (170 mobile),
        but `.media-row video:only-child{--mh:720px}` — which lifts a LONE attachment so a single photo can
        fill the feed column — has the same specificity (0,2,1) and comes later in the file, so it won the
        tie. A lone portrait video in a notification reserved 400x640 on a 390px phone and 288x461 on
        desktop. The reservation work made it stable, not small, so it was still the biggest jump in the
        app when the referenced post landed. It applied to the old max-height cascade too.
        """
        for label, vw, vh in VIEWPORTS:
            for r in self.rows("props", vw, vh):
                if not r["ctx"].startswith("notif"):
                    continue
                self.assertLessEqual(
                    r["reserved"][1], NOTIF_MAX_MEDIA_H,
                    f"{label} {r['ctx']} {r['key']} {r['w']}x{r['h']}: reserved {r['reserved']} — a "
                    f"notification row is not a feed card; a cap with wider specificity is winning")

    def test_a_wrong_guess_is_bounded_by_the_notification_cap(self):
        """With no imeta dim, media is reserved from a 16:10 guess and corrected on load. The guarantee in a
        notification is that the vertical correction CANNOT EXCEED THE CAP — so first sight settles by tens
        of pixels rather than the hundreds it used to.

        I twice over-claimed this while writing it, and both times the measurement was the corrective:
          - first as ZERO, reasoning that a tight cap fixes the height whatever the ratio. Only true when
            the cap binds for BOTH shapes; on a 390px phone the 240px quoted-carousel cap needs 384px of
            width for a 16:10 box and only ~348px is there, so the guess comes out width-bound (22.5px off).
          - then as "< 40px", which a 3.2:1 panorama breaks: it is far shorter than a 16:10 box at the same
            cap, giving 57.3px.
        The cap bound is the property that is actually true, and it is the one worth guarding: before the
        .notif-ctx cap was fixed, a lone portrait video could correct by ~470px.
        """
        for label, vw, vh in VIEWPORTS:
            for r in self.rows("props", vw, vh):
                if not r["ctx"].startswith("notif"):
                    continue
                dh = abs(r["guessReserved"][1] - r["reserved"][1])
                self.assertLessEqual(
                    dh, NOTIF_MAX_MEDIA_H,
                    f"{label} {r['ctx']} {r['key']} {r['w']}x{r['h']}: a 16:10 guess reserves "
                    f"{r['guessReserved']} but the real shape is {r['reserved']} — {dh:.1f}px of "
                    f"vertical correction on first sight, more than the cap allows, so a notification "
                    f"cap has loosened")

    def test_videos_are_covered(self):
        """The first version of this file emitted <img> for every case, which is how the notification video
        bug shipped. Fail loudly if the video cases silently stop being generated."""
        if not FFMPEG:
            self.skipTest("no ffmpeg on this node")
        vids = [r for r in self.rows("props", *VIEWPORTS[0][1:]) if r.get("kind") == "video"]
        self.assertGreaterEqual(len(vids), 2 * len(CONTEXTS), "video cases missing from the harness")

    def test_attribute_only_form_does_not_reserve(self):
        """width/height attributes alone reserve NOTHING, and this is the pin for that.

        They are presentational hints, so the author rule `width:auto` beats them. It is the obvious fix,
        it is what every 'add width and height to your images' article says, and here it does nothing —
        which is invisible unless you measure, because the markup looks correct.

        The probe is that the reserved box does not carry the declared SHAPE. "height > 0" is the wrong
        one: an empty <video> always has a 300x150 default object size, so it measured 218x110 for a
        landscape and a portrait clip alike — a box, but not the declared one, which is exactly the point.
        """
        for r in self.rows("attrs", *VIEWPORTS[0][1:]):
            want = r["w"] / r["h"]
            got = r["reserved"][0] / max(0.01, r["reserved"][1])
            # Either the box is negligible (an <img> collapses to its 2px border) or it is not the declared
            # shape (an empty <video> holds its 300x150 default whatever the attributes say). Both mean the
            # attributes conveyed nothing. The disjunction is needed because a 2x2 box is 1:1, which
            # coincidentally matches a SQUARE test image's declared ratio.
            negligible = r["reserved"][1] < 20
            wrong_shape = abs(got - want) >= 0.05
            self.assertTrue(
                negligible or wrong_shape,
                f"{r['ctx']} {r['key']} {r['w']}x{r['h']}: width/height attributes alone reserved "
                f"{r['reserved']}, the declared shape at a real size — if a CSS change made width "
                f"resolvable from the attributes, the inline --arn/--nw hint may be redundant")


if __name__ == "__main__":
    unittest.main()
