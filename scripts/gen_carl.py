#!/usr/bin/env python
"""Build assets/characters/carl.png — Carl Brutananadilewski, FULL BODY.

Run ONCE locally; production only ever loads the resulting PNG. Same reason as
scripts/gen_ruckus.py and scripts/gen_nodontthink.py: the asset's provenance is a script, not a
mystery binary in git.

This REPLACED a chest-up crop (826x650, right hand pointing off-frame). That art was sharper, but it
ended mid-torso, so over a photo he read as a bust pasted at the bottom edge rather than someone
standing in the frame — and the standing pose is what was asked for. The old file is in git history
if the pointing gesture is ever wanted back.

Unlike the ruckus source this one still HAS its alpha channel, so there is nothing to key: the whole
job is fetch, verify the alpha is real, and trim the transparent border so `height_frac` sizes Carl
rather than his padding.

The one real limitation is size. 243x462 is the largest full-body cutout of this art in circulation
(everything higher-resolution is face-only), so on a tall photo he is upscaled ~1.2x. That is
survivable here specifically because the art is flat cel fill with hard outlines, which Lanczos
handles without the mush it would make of a photo.

Usage:  venv-unified/bin/python scripts/gen_carl.py [source.png]
"""
import os
import sys

import numpy as np
from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "assets", "characters", "carl.png")
SRC = ("https://www.pngkey.com/png/full/"
       "326-3269360_carl-brutananadilewski-aqua-teen-hunger-force-carl.png")


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/carl_full.png"
    im = Image.open(src).convert("RGBA")
    a = np.asarray(im)[:, :, 3]
    print(f"source {src} {im.size}")
    if (a > 200).mean() > 0.97:
        # A "transparent PNG" from a clipart site is often a flattened JPEG in a PNG wrapper. That
        # would composite as an opaque rectangle over the photo — the same trap the end-card logo hit.
        print("source has no usable alpha (it is effectively opaque)", file=sys.stderr)
        return 1
    print(f"  opaque {(a > 200).mean()*100:.1f}%, {int(((a > 8) & (a < 200)).sum())} soft-edge px")

    bb = im.getbbox()
    out = im.crop(bb) if bb else im
    print(f"  figure {out.size} (aspect {out.size[0]/out.size[1]:.2f}) from {im.size}")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.save(OUT)
    print(f"wrote {OUT} {out.size}")

    chk = Image.new("RGB", out.size, (255, 0, 255))
    chk.paste(out, (0, 0), out)
    chk.save("/tmp/carl_on_magenta.png")
    print("magenta check: /tmp/carl_on_magenta.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
