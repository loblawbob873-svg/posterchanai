"""The Magic Eraser on what people actually brush out of memes: black lines, scribbles, black shapes.

Reported: "magic eraser not working, trying to remove black lines or shapes and it makes it worse and
black" / "or it does not remove the selected stuff". Measured: brush the middle of a black line and the
LaMa model redrew the line THROUGH the hole — a third of the brushed pixels came back dark — because it
continues structure that crosses the hole. The shipped tests only ever erased a solid square from a
stripe pattern, which is the case the model is best at.

Every case runs the real service (inpaint_service.inpaint); the model cases run the real LaMa model
when this node has it (it is fetched on first use) and say so when it does not.
"""
import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services import inpaint_service as S

W = H = 600


def _have_model():
    try:
        return bool(S.model_path()) and not S.model_blocked() and S._session() is not None
    except Exception:
        return False


METHODS = ["auto", "opencv", "diffusion"]


def _page(bg=(250, 250, 250)):
    return Image.new("RGBA", (W, H), bg + (255,))


def _photo():
    y, x = np.mgrid[0:H, 0:W]
    bg = np.stack([120 + 80 * np.sin(x / 60.0), 140 + 60 * np.cos(y / 50.0), 180 + 40 * np.sin((x + y) / 90.0)], -1)
    bg = np.clip(bg + np.random.RandomState(1).normal(0, 6, bg.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(bg).convert("RGBA"), bg


def _mask(draw):
    m = Image.new("L", (W, H), 0)
    draw(ImageDraw.Draw(m))
    return np.asarray(m) > 127


def _dark(out, where, limit=300):
    """Share of `where` that came out dark (RGB sum under `limit`)."""
    return float((out[..., :3].astype(int)[where].sum(-1) < limit).mean())


@pytest.mark.parametrize("method", METHODS)
def test_the_brushed_part_of_a_line_is_gone_not_redrawn(method):
    img = _page()
    ImageDraw.Draw(img).line([(50, 300), (550, 300)], fill=(0, 0, 0, 255), width=8)
    hole = _mask(lambda d: d.line([(220, 300), (380, 300)], fill=255, width=24))
    out, used = S.inpaint(np.asarray(img).copy(), hole, method=method)
    assert _dark(out, hole) < 0.03, f"{used} put the line back ({_dark(out, hole):.0%} of the brushed area dark)"


@pytest.mark.parametrize("method", METHODS)
def test_a_scribble_on_a_white_page_comes_out_white(method):
    img = _page()
    d = ImageDraw.Draw(img)
    pts = [(100 + i * 40, 250 + (60 if i % 2 else -60)) for i in range(10)]
    d.line(pts, fill=(0, 0, 0, 255), width=6, joint="curve")
    hole = _mask(lambda m: m.line(pts, fill=255, width=20, joint="curve"))
    out, used = S.inpaint(np.asarray(img).copy(), hole, method=method)
    assert _dark(out, hole) < 0.03, f"{used} left the scribble"
    assert out[..., :3][hole].mean() > 230, f"{used} filled a white page with grey/black"


@pytest.mark.parametrize("method", METHODS)
def test_black_lines_and_a_black_shape_on_a_photo(method):
    img, bg = _photo()
    d = ImageDraw.Draw(img)
    d.line([(50, 100), (550, 160)], fill=(0, 0, 0, 255), width=8)
    d.ellipse([250, 220, 330, 300], fill=(0, 0, 0, 255))
    ink = np.asarray(img)[..., :3].sum(-1) < 60

    def brush(m):
        m.line([(50, 100), (550, 160)], fill=255, width=14)
        m.ellipse([244, 214, 336, 306], fill=255)
    hole = _mask(brush)
    out, used = S.inpaint(np.asarray(img).copy(), hole, method=method)
    # The photo has dark areas of its own, so "black" here is near-black ink.
    assert _dark(out, ink, 120) == 0.0, f"{used} left black behind"
    assert np.abs(out[..., :3].astype(int)[ink] - bg.astype(int)[ink]).mean() < 15


@pytest.mark.parametrize("method", METHODS)
def test_a_line_across_a_cut_out_fills_with_the_cut_out_not_black(method):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))            # transparent = black RGB underneath
    d = ImageDraw.Draw(img)
    d.ellipse([100, 100, 500, 500], fill=(240, 150, 40, 255))
    d.line([(150, 300), (450, 300)], fill=(0, 0, 0, 255), width=10)
    hole = _mask(lambda m: m.line([(150, 300), (450, 300)], fill=255, width=22))
    rgba = np.asarray(img).copy()
    inside = hole & (rgba[..., 3] == 255)
    out, used = S.inpaint(rgba, hole, method=method)
    assert _dark(out, inside) == 0.0, f"{used} filled a cut-out with black"


@pytest.mark.skipif(not _have_model(), reason="the LaMa model is not on this node (fetched on first use)")
def test_real_object_removal_still_uses_the_model():
    """The guard must not throw the model away where it is the better answer: a solid object over a
    stripe pattern (the model continues the stripes; the plain fill smears them)."""
    y, x = np.mgrid[0:H, 0:W]
    stripes = np.where((x // 16) % 2 == 0, 30, 220).astype(np.uint8)
    base = np.stack([stripes, stripes, stripes], -1)
    img = Image.fromarray(base).convert("RGBA")
    ImageDraw.Draw(img).rectangle([250, 250, 350, 350], fill=(220, 30, 30, 255))
    hole = _mask(lambda m: m.rectangle([244, 244, 356, 356], fill=255))
    out, used = S.inpaint(np.asarray(img).copy(), hole, method="auto")
    assert used == "lama", "the guard discarded the model on ordinary object removal"
    red = (out[..., 0].astype(int) - out[..., 1].astype(int))[hole]
    assert (red > 100).mean() < 0.01, "the object came back"


def test_the_bytes_the_builder_sends_erase_a_line_on_a_large_picture():
    """End to end through magic_erase: the picture as the layer stores it, and the mask the way the
    builder's canvas exports it — white strokes on TRANSPARENT, at the builder's reduced mask size
    (MASK_EDGE), not the picture's — so the scaling and polarity are exercised, not assumed."""
    import io
    big_w, big_h = 1600, 1200
    img = Image.new("RGB", (big_w, big_h), (245, 240, 230))
    ImageDraw.Draw(img).line([(100, 600), (1500, 600)], fill=(0, 0, 0), width=14)
    ImageDraw.Draw(img).ellipse([700, 200, 900, 400], fill=(0, 0, 0))
    src = io.BytesIO(); img.save(src, "JPEG", quality=92)
    k = 1024 / max(big_w, big_h)                        # the builder's MASK_EDGE cap
    mw, mh = round(big_w * k), round(big_h * k)
    m = Image.new("RGBA", (mw, mh), (0, 0, 0, 0))
    d = ImageDraw.Draw(m)
    d.line([(100 * k, 600 * k), (1500 * k, 600 * k)], fill=(255, 255, 255, 255), width=round(34 * k))
    d.ellipse([690 * k, 190 * k, 910 * k, 410 * k], fill=(255, 255, 255, 255))
    mk = io.BytesIO(); m.save(mk, "PNG")
    png, used = S.magic_erase(src.getvalue(), mk.getvalue())
    out = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
    assert out.shape[:2] == (big_h, big_w), "the result is not the layer's size"
    ink = np.asarray(img).sum(-1) < 120
    assert _dark(out, ink, 300) < 0.01, f"{used} left the line/shape ({_dark(out, ink, 300):.1%} dark)"
