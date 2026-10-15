"""The Bad Post of the Day facepalm: one connected arm, and the shipped PNG is what the script builds.

Asked for 2026-10-14: "improve the face palm graphic". The old picture pasted dance-2's waving palm over her
eyes with its forearm CUT OFF halfway down the hoodie -- an arm that ends in mid-air -- with a pale, jagged
halo from cutting the art out at its native size and then magnifying it 3x. The hand is now the mirrored
arm (the back of her right hand), and its forearm runs off the bottom edge of the frame.

Measured on the picture people actually see: the skin under her palm is followed, pixel to pixel, and must
leave the frame at the bottom on her right side. The old image's arm stopped ~190 px above the edge.
"""
import importlib.util
import os

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PNG = os.path.join(ROOT, "static", "mascot", "badpost-facepalm.png")


def _script():
    spec = importlib.util.spec_from_file_location("make_badpost_facepalm",
                                                  os.path.join(ROOT, "scripts", "make_badpost_facepalm.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _palm_xy(mod):
    fig_w = round((mod.BUST[2] - mod.BUST[0]) * mod.SCALE)
    fig_h = round((mod.BUST[3] - mod.BUST[1]) * mod.SCALE)
    x0, y0 = (mod.SIZE - fig_w) // 2, mod.SIZE - fig_h
    return (round(x0 + (mod.PALM_AT[0] - mod.BUST[0]) * mod.SCALE),
            round(y0 + (mod.PALM_AT[1] - mod.BUST[1]) * mod.SCALE))


def test_the_hand_over_her_eyes_belongs_to_an_arm_that_leaves_the_frame():
    mod = _script()
    px = np.asarray(Image.open(PNG).convert("RGB")).astype(int)
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    skin = (r > 200) & (g > 165) & (b > 135) & (r - b > 22) & (r - b < 110)   # warm: not the white caption
    labels, _ = ndimage.label(skin)
    x, y = _palm_xy(mod)
    # The palm centre itself may fall on a knuckle line; take the skin component nearest to it.
    win = labels[y - 12:y + 13, x - 12:x + 13]
    ids = [v for v in np.unique(win) if v]
    assert ids, ("no skin under the palm -- the hand is not over her eyes", (x, y))
    arm = max(ids, key=lambda v: int((labels == v).sum()))
    comp = labels == arm
    # Her right forearm leaves the frame on HER right (the viewer's left), well clear of her neck: the skin
    # under the palm is otherwise joined to the bottom edge only down her neckline, which is what the old
    # picture's cut-off arm amounted to.
    bottom = np.where(comp[mod.SIZE - 20:].any(axis=0))[0]
    assert len(bottom) and bottom.min() < x - 80, ("the hand over her eyes is not joined to an arm leaving the "
                                                   "frame -- a forearm cut off in mid-air", bottom[:5].tolist())
    rows = np.where(comp.any(axis=1))[0]
    assert rows.min() < y, "the skin found is not the hand over her face"


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
