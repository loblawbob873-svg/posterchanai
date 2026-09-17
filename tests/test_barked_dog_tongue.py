"""`barked`: the dog's tongue is a tongue, not a red circle with a line through it.

Reported as "black line in red circle, ruins the tongue". The tongue was a whole outlined disc drawn ON
the lip with a darker crease down its middle. It now hangs from under the lip with no crease. Measured
on the rendered pixels: the inside of the pink shape holds no stroke.
"""
from app.services.effects_service.stamps import _make_barked_dog


def _is_pink(p):
    r, g, b, a = p
    return a > 200 and r > 170 and r - g > 60 and r - b > 50


def test_the_tongue_has_no_line_through_it():
    for size in (200, 400, 900):
        tile = _make_barked_dog(size)
        W, H = tile.size
        px = tile.load()
        pts = [(x, y) for y in range(int(H * 0.6), int(H * 0.9))
               for x in range(int(W * 0.5), int(W * 0.8)) if _is_pink(px[x, y])]
        assert pts, "no tongue drawn"
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        assert (x1 - x0) > W * 0.05 and (y1 - y0) > H * 0.05, "tongue too small to read"
        # The core of the tongue: shrink its box by a third on each side.
        cx0, cx1 = x0 + (x1 - x0) // 3, x1 - (x1 - x0) // 3
        cy0, cy1 = y0 + (y1 - y0) // 3, y1 - (y1 - y0) // 3
        dark = [(x, y) for y in range(cy0, cy1 + 1) for x in range(cx0, cx1 + 1)
                if px[x, y][3] > 0 and px[x, y][0] < 200]
        assert not dark, f"{size}px: a stroke runs through the tongue at {dark[:3]}"


def test_the_tongue_hangs_below_the_lip():
    """It pokes out from under the mouth line, so most of it sits below the smirk corner."""
    tile = _make_barked_dog(400)
    px = tile.load()
    pink_rows = [y for y in range(240, 360) if any(_is_pink(px[x, y]) for x in range(200, 320))]
    assert min(pink_rows) >= int(400 * 0.66)
    assert max(pink_rows) >= int(400 * 0.76)
