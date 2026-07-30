#!/usr/bin/env python
"""Build assets/characters/ruckus.png — Uncle Ruckus, checkerboard background removed.

Run ONCE locally; production only ever loads the resulting PNG. This exists so the asset's
provenance is reproducible rather than a mystery binary in git (same reason as
scripts/gen_nodontthink.py and scripts/gen_pointing_chars.py).

The source is a PNG cutout that was re-encoded as a JPEG, so its transparency is BAKED IN as a
literal grey/white checkerboard — the alpha channel is gone and the "background" is now pixels. That
rules out both of the usual routes: there is no alpha to keep, and a plain colour key would eat his
white T-shirt and the light grey of his belly along with the board. So the board is found by
CONNECTIVITY instead: near-neutral pixels at checkerboard luminance that are reachable from the frame
edge. His shirt is the same white but enclosed by his jacket, so it is never reached.

JPEG ringing leaves a one-or-two-pixel halo of intermediate greys around his outline, which a hard
mask keeps as a bright fringe over a dark photo. Shrinking the matte by a pixel and feathering it
turns that fringe into the edge itself.

Usage:  venv-unified/bin/python scripts/gen_ruckus.py [source.jpg]
"""
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "assets", "characters", "ruckus.png")
SRC = "https://i.pinimg.com/originals/5d/31/20/5d3120e12de7e8bdf0c4449bdbd32dfa.jpg"


def board_alpha(im: Image.Image) -> np.ndarray:
    """Soft alpha (uint8) — opaque everywhere except the baked-in checkerboard."""
    a = np.asarray(im).astype(np.int16)
    spread = a.max(axis=2) - a.min(axis=2)          # 0 for any pure grey
    lum = a.mean(axis=2)
    # The two board tones measured off this source are 255 and 204; the band covers the JPEG's
    # wobble around them without reaching his khakis (~180) or the white shirt's shading.
    board = (spread < 14) & (lum > 196)

    # Only the board pixels the frame EDGE can reach. Flood-filling by connectivity is what keeps his
    # shirt: it is the same white, but enclosed.
    lab, n = ndimage.label(board)
    edge = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
    edge.discard(0)
    outside = np.isin(lab, list(edge))
    print(f"  {n} neutral regions, {len(edge)} touch the edge, board covers "
          f"{outside.mean()*100:.1f}% of the frame")

    alpha = np.where(outside, 0.0, 1.0)
    # Erode by a pixel before feathering: the JPEG halo is OUTSIDE his outline, so growing the
    # transparent side is what removes it. Feathering alone would only make the fringe softer.
    alpha = ndimage.minimum_filter(alpha, size=3)
    alpha = ndimage.gaussian_filter(alpha, 0.7)
    return (np.clip(alpha, 0, 1) * 255).astype(np.uint8)


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ruckus_src.jpg"
    im = Image.open(src).convert("RGB")
    print(f"source {src} {im.size}")
    alpha = board_alpha(im)

    # Drop anything not attached to him — a stray speck of near-white that the flood could not reach
    # (an interior board square peeking through a gap, say) is invisible to the eye but not to
    # getbbox(), and empty padding in the asset makes the figure composite small. Same trap as
    # nodontthinkiwill; threshold at the alpha the crop below treats as "present".
    lab, n = ndimage.label(alpha > 8)
    if n >= 1:
        sizes = ndimage.sum(np.ones_like(lab), lab, range(1, n + 1))
        alpha = np.where(lab == int(np.argmax(sizes)) + 1, alpha, 0)
        if n > 1:
            print(f"  dropped {n - 1} region(s) not connected to the subject")

    rgba = np.dstack([np.asarray(im).astype(np.uint8), alpha])
    ys, xs = np.where(alpha > 8)
    if not len(ys):
        print("nothing left after keying", file=sys.stderr)
        return 1
    # Full body, no bust crop: at 0.47 aspect he sits between `would` (0.42) and `shrug` (0.72), so
    # _add_pointing_meme's width cap never bites and the standing pose survives.
    out = Image.fromarray(rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1], "RGBA")
    print(f"  figure {out.size} (aspect {out.size[0]/out.size[1]:.2f}) from full {im.size}")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.save(OUT)
    print(f"wrote {OUT} {out.size}")

    chk = Image.new("RGB", out.size, (255, 0, 255))
    chk.paste(out, (0, 0), out)
    chk.save("/tmp/ruckus_on_magenta.png")
    print("magenta check: /tmp/ruckus_on_magenta.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
