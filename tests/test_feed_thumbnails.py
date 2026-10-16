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


def test_a_big_picture_is_resized_and_small_or_animated_ones_are_not():
    out = thumb_service.resize(_png(1500, 1500), 160)
    im = Image.open(io.BytesIO(out))
    assert im.format == "WEBP" and im.size == (160, 160)
    assert thumb_service.resize(_png(100, 80), 160) is None, "an already-small picture is its own thumbnail"
    g = io.BytesIO()
    frames = [Image.new("RGB", (800, 800), c) for c in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(g, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    assert thumb_service.resize(g.getvalue(), 160) is None, "an animation must stay an animation"
    assert thumb_service.resize(b"not an image", 160) is None


def test_the_route(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(thumb_service, "_ROOT", str(tmp_path))
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        return _png(2000, 1000) if "big" in url else None
    monkeypatch.setattr(thumb_service, "_fetch", fake_fetch)
    c = TestClient(app)
    r = c.get("/client/thumb", params={"u": "https://pics.example/big.png", "w": 132}, headers={"Sec-Fetch-Dest": "image"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(r.content)).size == (160, 80)
    assert "nosniff" in r.headers.get("x-content-type-options", "")
    c.get("/client/thumb", params={"u": "https://pics.example/big.png", "w": 140}, headers={"Sec-Fetch-Dest": "image"})
    assert len(fetched) == 1, "the second request for the same size must come from the cache"
    r = c.get("/client/thumb", params={"u": "https://pics.example/gone.png"}, headers={"Sec-Fetch-Dest": "image"},
              follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://pics.example/gone.png"
    r = c.get("/client/thumb", params={"u": "https://evil.example/phish"}, headers={"Sec-Fetch-Dest": "document"},
              follow_redirects=False)
    assert r.status_code == 400, "a navigation must never be redirected off this domain"
    assert c.get("/client/thumb", params={"u": "file:///etc/passwd"}).status_code == 400


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
        assert o[k]["gif"] == "https://h.example/a.gif" and o[k]["data"].startswith("data:")
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
