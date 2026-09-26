"""`cum` / `blood` on a phone photo must finish in seconds, not minutes.

Reported as "effects are not working after you choose effect, failed many times". The renders were
not failing, they were SLOW: a 12 MP phone photo took ~60 s (`cum`) and ~80 s (`blood`) on an idle
box, and 3-5 minutes on a busy node, so the user gave up and tried again, and each new try opened
another Effects conversation and abandoned the result of the one before.

Both splatters run Pillow rank filters (MaxFilter/MinFilter) on every tile. A rank filter costs
kernel² per pixel, and both the kernel and the tile scale with the photo, so the cost grows with
the fourth power of the tile size. `_scatter_overlay` now renders those tiles at a capped size and
scales the finished tile up (they are soft, blurred blobs, so nothing is lost). These tests pin the
cap on the size the tile renderer is ASKED for, which is what makes it fast, and check that the output
is still the full-size photo with splatter on it.
"""
import io
import random

import numpy as np
from PIL import Image

from app.services.effects_service import gore
from app.services.effects_service import _common


def _photo(w=4000, h=3000):
    rng = np.random.default_rng(1)
    arr = (rng.random((h, w, 3)) * 120).astype("uint8")   # darkish, so off-white splats show
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "JPEG", quality=85)
    return b.getvalue()


def _run(monkeypatch, add, maker_name):
    asked = []
    real = getattr(gore, maker_name)

    def spy(size, *a, **k):
        asked.append(size)
        return real(size, *a, **k)

    monkeypatch.setattr(gore, maker_name, spy)
    random.seed(7)
    data = _photo()
    out = add(data)
    return data, out, asked


def _check(data, out, asked):
    assert asked, "the tile renderer was never called"
    assert max(asked) <= _common.SCATTER_TILE_CAP, (
        f"a tile was rendered at {max(asked)}px — the cap is what keeps a 12 MP photo to seconds")
    src = Image.open(io.BytesIO(data))
    res = Image.open(io.BytesIO(out))
    assert res.size == src.size, "the result must stay the full photo size"
    diff = np.abs(np.asarray(res.convert("RGB"), dtype=int) - np.asarray(src.convert("RGB"), dtype=int))
    changed = (diff.max(axis=2) > 60).mean()
    assert changed > 0.05, f"only {changed:.1%} of the photo changed — splatters should be visible"


def test_cum_renders_capped_tiles_at_full_size(monkeypatch):
    _check(*_run(monkeypatch, gore.add_cum, "_make_cum"))


def test_blood_renders_capped_tiles_at_full_size(monkeypatch):
    _check(*_run(monkeypatch, gore.add_blood, "_make_blood"))


def _edge_alpha(tile):
    a = np.asarray(tile)[..., 3]
    return max(int(a[0].max()), int(a[-1].max()), int(a[:, 0].max()), int(a[:, -1].max()))


def test_splatter_tiles_are_not_clipped_by_their_own_edge():
    """A strand reaching past the tile's square edge was cut off there, so every splat on the photo
    carried a straight-edged clip (visible as hard diagonal lines through the picture). The tile's
    border must be fully transparent: the whole splatter fits inside it."""
    for maker in (gore._make_cum, gore._make_blood):
        for seed in range(40):
            tile = maker(_common.SCATTER_TILE_CAP, rng=random.Random(seed))
            assert _edge_alpha(tile) == 0, f"{maker.__name__} seed {seed}: splat clipped at the tile edge"
