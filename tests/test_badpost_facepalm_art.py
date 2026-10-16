"""The Bad Post of the Day facepalm: a hand of normal size, BACK to the viewer, over BOTH eyes, out of her sleeve --
and the shipped PNG is what the script builds.

History: the first picture pasted dance-2's waving palm over her eyes with its forearm cut off in mid-air; the
second ran that bare arm off the bottom edge -- "the hand is backwards and longer than normal" (2026-10-16): it was
the PALM (the waving arm mirrored) and a bare forearm the length of her torso, though she wears a long-sleeved
hoodie. The hand is now drawn: back of the right hand, thumb on the outside, the wrist in the hoodie cuff.

Measured on the picture people actually see.
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


def _at(mod, dx, dy):
    """dance-5 coordinates -> pixel in the shipped picture."""
    fig_w = round((mod.BUST[2] - mod.BUST[0]) * mod.SCALE)
    fig_h = round((mod.BUST[3] - mod.BUST[1]) * mod.SCALE)
    x0, y0 = (mod.SIZE - fig_w) // 2, mod.SIZE - fig_h
    return round(x0 + (dx - mod.BUST[0]) * mod.SCALE), round(y0 + (dy - mod.BUST[1]) * mod.SCALE)


def _skin(px):
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    return (r > 200) & (g > 165) & (b > 135) & (r - b > 22) & (r - b < 110)   # warm: not the white caption


def test_one_normal_sized_hand_covers_both_eyes_and_comes_out_of_her_sleeve():
    mod = _script()
    px = np.asarray(Image.open(PNG).convert("RGB")).astype(int)
    labels, _ = ndimage.label(_skin(px))
    eyes = getattr(mod, "EYES", None) or mod.PALM_AT
    hand = np.zeros(labels.shape, dtype=bool)
    for dx in (-7, 7):                                     # her two eyes, ~7 dance-5 px either side of centre
        x, y = _at(mod, eyes[0] + dx, eyes[1])
        win = labels[y - 6:y + 7, x - 6:x + 7]
        ids = [v for v in np.unique(win) if v]
        assert ids, ("an eye is not covered by the hand", dx, (x, y))
        for v in ids:                                      # fingers and the back are outlined apart: their union
            hand |= labels == v
    rows, cols = np.where(hand.any(axis=1))[0], np.where(hand.any(axis=0))[0]
    face = 30 * mod.SCALE                                  # her face is ~30 dance-5 px tall
    assert rows.max() - rows.min() < 1.9 * face, ("the hand is far longer than her face: a bare arm, not a hand",
                                                  rows.max() - rows.min(), face)
    # below the hand: the dark sleeve, connected to the bottom edge of the picture
    dark = px.sum(axis=2) < 150
    dlab, _ = ndimage.label(dark)
    wrist_y = rows.max()
    cx = int(np.mean(np.where(hand[wrist_y - 3])[0]))
    below = [v for v in np.unique(dlab[wrist_y + 4:wrist_y + 20, cx - 12:cx + 12]) if v]
    assert below, "nothing dark under the wrist: the hand is not in her sleeve"
    assert any(dlab[mod.SIZE - 1][dlab[mod.SIZE - 1] == v].size for v in below), "the sleeve does not leave the frame"
    # the BACK of her right hand: the thumb sits on the viewer's left, below the fingertips
    left = cols.min()
    assert hand[:, left].any() and np.where(hand[:, left])[0].mean() > rows.min() + 0.25 * (rows.max() - rows.min()), \
        "the thumb is not on the outside (viewer's left): the hand is backwards"


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
