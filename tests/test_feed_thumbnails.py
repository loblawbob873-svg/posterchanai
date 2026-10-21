"""Feed pictures and avatars are served at the size they are drawn (Vyram's choppy APK feed, 2026-10-16).

Measured on poster.place's Nostrverse at phone width: the feed's images held 619 MB decoded against 72 MB needed
-- 111 avatars of 22-44 px were 319 MB of it. `/client/thumb` resizes on the instance; these hold its rules and
the client helper that chooses it:
  * the ladder, the resize itself, and that an animation / an already-small picture / an unreadable one is NOT
    resized (the route sends the client to the original -- what it did before);
  * the redirect is only for an image load (anything else would be an open redirect off this domain);
  * the SHIPPED PCThumb (app.js) asks for a thumbnail on an instance, never without one, never for GIF/SVG, and a
    failed thumbnail falls back to the original before any other fallback.
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import tempfile

import pytest
from PIL import Image

from app.services import thumb_service

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _png(w, h):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(b, "PNG")
    return b.getvalue()


def test_the_ladder():
    assert [thumb_service.snap(x) for x in (1, 48, 49, 132, 600, 866, 5000, "x")] == [48, 48, 96, 160, 720, 1080, 1080, 320]


def test_a_big_picture_is_resized_and_a_small_one_is_not():
    out = thumb_service.resize(_png(1500, 1500), 160)
    im = Image.open(io.BytesIO(out))
    assert im.format == "WEBP" and im.size == (160, 160)
    assert thumb_service.resize(_png(100, 80), 160) is None, "an already-small picture is its own thumbnail"
    assert thumb_service.resize(b"not an image", 160) is None


def _gif(w, h, n, noise=True):
    """A GIF shaped like real feed GIFs: smooth, dithered content (GIF's 256-colour palette dithers it, which is why
    such GIFs are huge and their WebP small) -- or, with noise=False, flat colour WebP cannot beat."""
    frames = []
    for i in range(n):
        if noise:
            frames.append(Image.linear_gradient("L").resize((w, h)).rotate(i * 7).convert("RGB"))
        else:
            frames.append(Image.new("RGB", (w, h), (i * 40 % 255, 80, 160)))
    g = io.BytesIO()
    frames[0].save(g, "GIF", save_all=True, append_images=frames[1:], duration=[80 + 10 * i for i in range(n)], loop=0)
    return g.getvalue()


def test_an_animation_becomes_a_smaller_animation_with_its_timing():
    """2026-10-21: GIFs were the single biggest bytes on a phone's feed, 1-2 MB decoded at full size every frame
    while scrolling. An animation is now an animated WebP at the drawn width -- still an animation, same frames,
    same per-frame durations, still looping."""
    src = _gif(800, 600, 6)
    out = thumb_service.resize(src, 320)
    assert out is not None, "a big animated GIF was served at full size"
    im = Image.open(io.BytesIO(out))
    assert im.format == "WEBP" and im.is_animated and im.n_frames == 6, "it must stay an animation"
    assert im.size == (320, 240)
    durs = []
    for i in range(im.n_frames):
        im.seek(i); im.load(); durs.append(im.info.get("duration"))   # WebP fills a frame's info once it is decoded
    assert durs == [80 + 10 * i for i in range(6)], durs
    assert len(out) < len(src)


def test_a_gif_no_wider_than_asked_is_still_re_encoded_when_that_saves_bytes():
    """The measured case: a 315px, 46-frame feed GIF was 1.99 MB; its same-size WebP 303 KB."""
    src = _gif(300, 200, 8)
    out = thumb_service.resize(src, 640)
    assert out is not None and Image.open(io.BytesIO(out)).size == (300, 200), "never upscaled, still re-encoded"
    assert len(out) < 0.8 * len(src)


def test_an_animation_webp_cannot_shrink_or_that_is_too_big_is_served_as_it_is(monkeypatch):
    flat = _gif(64, 64, 2, noise=False)
    assert thumb_service.resize(flat, 640) is None, "a WebP that does not save 20% is not worth serving"
    monkeypatch.setattr(thumb_service, "ANIM_MAX_FRAMES", 3)
    assert thumb_service.resize(_gif(800, 600, 6), 320) is None, "past the frame bound the original is served"


def test_the_route(monkeypatch, tmp_path):
    """No request ever waits for a resize ("vyram said it's worse now", 2026-10-16: a new picture cost ~2.5 s inline,
    3 at a time, and the service worker's own re-fetch got a 400 after paying it -- so it was paid twice)."""
    import time
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.client import router
    # Just the route, never the whole app: `with TestClient(app)` runs the app's STARTUP, whose background table
    # loads never finish in a test and outlive it -- the next test in the process (the stats bot's) then read
    # "table fedi_puppets is still loading" (deploy 256's gate, 2026-10-16). The `with` is needed so the
    # background resize keeps running between requests.
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(thumb_service, "_ROOT", str(tmp_path))
    fetched = []

    async def slow_fetch(url):
        fetched.append(url)
        await asyncio.sleep(3.0)                    # a far host and a big original
        return _png(2000, 1000) if "big" in url else None
    monkeypatch.setattr(thumb_service, "_fetch", slow_fetch)
    img = {"Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors"}
    worker = {"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"}          # the service worker's own fetch
    with TestClient(app) as c:
        t = time.monotonic()
        r = c.get("/client/thumb", params={"u": "https://pics.example/big.png", "w": 132}, headers=img, follow_redirects=False)
        # Clearly less than the 3 s the resize takes (the first request also pays the app's own warm-up).
        assert time.monotonic() - t < 1.5, "the first view waited for the resize"
        assert r.status_code == 302 and r.headers["location"] == "https://pics.example/big.png"
        assert "no-store" in r.headers.get("cache-control", ""), "a not-yet-made redirect must not be cached"
        r = c.get("/client/thumb", params={"u": "https://pics.example/big.png", "w": 132}, headers=worker, follow_redirects=False)
        assert r.status_code == 302, "the service worker's fetch must be redirected, not refused"
        for _ in range(80):                         # the background resize lands
            time.sleep(0.1)
            r = c.get("/client/thumb", params={"u": "https://pics.example/big.png", "w": 140}, headers=img, follow_redirects=False)
            if r.status_code == 200:
                break
        assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
        assert Image.open(io.BytesIO(r.content)).size == (160, 80)
        assert "nosniff" in r.headers.get("x-content-type-options", "")
        assert len(fetched) == 1, "one picture, one fetch -- however many requests asked for it meanwhile"
        c.get("/client/thumb", params={"u": "https://pics.example/gone.png"}, headers=img, follow_redirects=False)
        for _ in range(40):
            time.sleep(0.1)
            if thumb_service.peek("https://pics.example/gone.png", 320) is not None:
                break
        r = c.get("/client/thumb", params={"u": "https://pics.example/gone.png"}, headers=img, follow_redirects=False)
        assert r.status_code == 302 and "max-age" in r.headers.get("cache-control", ""), "a known 'original is right'"
        for h in ({"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"}, {}):
            r = c.get("/client/thumb", params={"u": "https://evil.example/phish"}, headers=h, follow_redirects=False)
            assert r.status_code == 400, "a navigation must never be redirected off this domain"
        assert c.get("/client/thumb", params={"u": "file:///etc/passwd"}, headers=img).status_code == 400


NODE = shutil.which("node")
RUN = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const a = src.indexOf('window.PCThumb = (function(){'), b = src.indexOf('})();', a) + 5;
const out = {};
for (const [name, base] of [['pwa', undefined], ['apk', 'https://poster.place'], ['standalone', '']]) {
  const window = { devicePixelRatio: 2.75 }; if (base !== undefined) window.__PC_API_BASE__ = base;
  const ctx = { window, location: { origin: 'https://poster.place' } };
  vm.runInNewContext(src.slice(a, b), ctx);
  const T = window.PCThumb;
  out[name] = { av: T.src('https://blossom.primal.net/x.png', 48), gif: T.src('https://h.example/a.gif', 48),
                data: T.src('data:image/png;base64,AA', 48), again: T.src(T.src('https://h.example/p.jpg', 48), 48) };
}
const img = { dataset: { osrc: 'https://h.example/p.jpg' }, _src: 'https://poster.place/client/thumb?w=1&u=x',
  getAttribute(){ return this._src; }, set src(v){ this._src = v; }, get src(){ return this._src; } };
const T = (() => { const window = { __PC_API_BASE__: 'https://poster.place' }; vm.runInNewContext(src.slice(a, b), { window, location: {} }); return window.PCThumb; })();
out.first = T.err(img, '/logo.png'); out.after1 = img.src; out.second = T.err(img, '/logo.png'); out.after2 = img.src;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_the_shipped_helper_picks_thumbnails_only_where_they_exist():
    with tempfile.TemporaryDirectory() as d:
        js = os.path.join(d, "run.cjs")
        open(js, "w").write(RUN)
        r = subprocess.run([NODE, js, os.path.join(ROOT, "static/js/client/app.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    want = "https://poster.place/client/thumb?w=132&u=https%3A%2F%2Fblossom.primal.net%2Fx.png"
    assert o["pwa"]["av"] == want and o["apk"]["av"] == want
    assert o["standalone"]["av"] == "https://blossom.primal.net/x.png", "no instance: the original"
    for k in ("pwa", "apk"):
        assert o[k]["gif"].startswith("https://poster.place/client/thumb?"), "a GIF goes through the resizer too"
        assert o[k]["data"].startswith("data:")
        assert o[k]["again"].count("/client/thumb?") == 1, "a thumbnail URL is never wrapped twice"
    assert o["first"] is True and o["after1"] == "https://h.example/p.jpg", "a failed thumbnail loads the original"
    assert o["second"] is False and o["after2"] == "/logo.png", "a failed original falls back as before"


def test_every_feed_avatar_and_picture_goes_through_it():
    cards = open(os.path.join(ROOT, "static/js/client/cards.js")).read()
    links = open(os.path.join(ROOT, "static/js/client/linkcards.js")).read()
    assert cards.count("PCThumb.src(") >= 4 and "PCThumb.src(av, 48)" in links
    assert "onerror=\"this.src='${S.LOGO}'\"" not in cards.replace("_S.LOGO", ""), "an avatar left on the original"
    assert "openLightbox(im.dataset.osrc||" in cards, "the lightbox must open the ORIGINAL picture"
    keys = open(os.path.join(ROOT, "static/js/client/keys.js")).read()
    assert "el.dataset.osrc||el.currentSrc" in keys
