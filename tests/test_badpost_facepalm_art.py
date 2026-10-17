"""The Bad Post of the Day card: PosterChan sad and crying -- no hand -- and the shipped PNG is what the script builds.

History: three facepalm hands in a row read as wrong (a pasted waving palm, then a bare arm "longer than normal",
then a drawn back-of-hand); "just make her look sad and crying, stop with the hand crap" (2026-10-16). Her face is
now redrawn: worried brows, eyes squeezed shut, tears down both cheeks, a frown.

Measured on the picture people actually see.
"""
import importlib.util
import os

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PNG = os.path.join(ROOT, "static", "mascot", "badpost-facepalm.png")


def _script():
    spec = importlib.util.spec_from_file_location("make_badpost_facepalm",
                                                  os.path.join(ROOT, "scripts", "make_badpost_facepalm.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _at(mod, dx, dy):
    """dance-5 coordinates -> pixel in the shipped picture."""
    fig_w = round((mod.BUST[2] - mod.BUST[0]) * mod.SCALE)
    fig_h = round((mod.BUST[3] - mod.BUST[1]) * mod.SCALE)
    x0, y0 = (mod.SIZE - fig_w) // 2, mod.SIZE - fig_h
    return round(x0 + (dx - mod.BUST[0]) * mod.SCALE), round(y0 + (dy - mod.BUST[1]) * mod.SCALE)


def _skin(px):
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    return (r > 200) & (g > 165) & (b > 135) & (r - b > 22) & (r - b < 110)   # warm: not the white caption


def test_she_is_crying_with_her_eyes_shut_and_no_hand_over_her_face():
    mod = _script()
    px = np.asarray(Image.open(PNG).convert("RGB")).astype(int)
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    tear = (b > 220) & (g > 180) & (r < 200) & (b - r > 40)
    sleeve = px.sum(axis=2) < 150
    u = mod.SCALE
    for side in (-1, 1):                                   # each eye: a tear running down that cheek
        x, y = _at(mod, mod.EYES[0] + side * 11, mod.EYES[1])
        cheek = tear[y + round(4 * u):y + round(20 * u), x - round(6 * u):x + round(6 * u)]
        rows = np.where(cheek.any(axis=1))[0]
        assert rows.size and rows.max() - rows.min() > 8 * u, ("no tear stream down the cheek", side)
        # eye shut: no open eye (its orange iris) left at the eye
        eye = px[y - round(3 * u):y + round(4 * u), x - round(5 * u) - side * round(3 * u):x + round(5 * u) - side * round(3 * u)]
        iris = (eye[..., 0] > 170) & (eye[..., 0] - eye[..., 2] > 120) & (eye[..., 1] < 160)
        assert iris.sum() < 0.03 * iris.size, ("an open eye is still drawn", side, int(iris.sum()))
    # no hand: nothing dark (a sleeve) or skin-coloured rises from below over her face's middle
    fx, fy = _at(mod, mod.EYES[0], mod.EYES[1])
    assert not sleeve[fy + round(2 * u):fy + round(14 * u), fx - round(2 * u):fx + round(2 * u)].any(), \
        "something dark crosses her face: a hand/sleeve is still drawn"


def test_the_shipped_png_is_what_the_script_builds(tmp_path, monkeypatch):
    """The image is derived from the mascot art by the script; a hand-edited PNG would be lost on its next run."""
    mod = _script()
    out = tmp_path / "facepalm.png"
    monkeypatch.setattr(mod, "OUT", str(out))
    mod.main()
    a = np.asarray(Image.open(out).convert("RGB")).astype(int)
    b = np.asarray(Image.open(PNG).convert("RGB")).astype(int)
    assert a.shape == b.shape
    assert np.abs(a - b).max() <= 2, "static/mascot/badpost-facepalm.png was not rebuilt from the script"


def test_no_hard_blue_gloom_lines_cross_her_head():
    """"what are those long blue vertical lines? ugly" (2026-10-15): the gloom was a fan of saturated blue
    strokes over her forehead. Now a soft shadow, so no run of that cold blue may stand anywhere in the
    picture (the sweat drop is a paler, greener blue and is not counted)."""
    import numpy as np
    from PIL import Image
    a = np.asarray(Image.open(PNG).convert("RGB")).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    blue = (b > 200) & (r < 150) & (g < 170)
    tallest = max(int(np.max(np.diff(np.flatnonzero(np.r_[False, col, False]))[::2], initial=0))
                  if col.any() else 0 for col in blue.T)
    assert tallest < 12, "a vertical blue stroke %d px tall is drawn on the facepalm card" % tallest
