#!/usr/bin/env python
"""Build assets/characters/nodontthinkiwill.png — old Steve Rogers, background removed.

Run ONCE locally; production only ever loads the resulting PNG. This exists so the asset's
provenance is reproducible rather than a mystery binary in git (same reason as
scripts/gen_pointing_chars.py).

Why segmentation and not a colour key: unlike the MS-Paint characters (`nothingeverhappens`,
`theraped`), this is a live-action frame on a blurred forest background — there is no key colour, so
the subject has to be found rather than subtracted. torchvision's deeplabv3_resnet101 with COCO
weights has `person` as class 15, which is exactly the query.

Two things that matter for the result:
  * Alpha comes from the SOFTMAX probability, not the argmax mask. A hard mask gives a
    cookie-cutter edge that reads as pasted-on over a photo; the probability ramp at the boundary is
    a usable matte for free.
  * Runs on CPU deliberately. This box shares one GPU between chat/image/music/video and holds a
    lock for it — a one-off 1422x800 forward pass is not worth contending for, or worth evicting a
    loaded LLM over.

Usage:  venv-unified/bin/python scripts/gen_nodontthink.py [source.jpg]
"""
import os
import sys

import numpy as np
import torch
from PIL import Image
from scipy import ndimage
from torchvision.models.segmentation import deeplabv3_resnet101
from torchvision import transforms

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "assets", "characters", "nodontthinkiwill.png")
PERSON_CLASS = 15          # COCO / VOC label used by the torchvision segmentation weights


def person_alpha(im: Image.Image) -> np.ndarray:
    """Soft alpha (uint8) for the largest person in the frame."""
    model = deeplabv3_resnet101(weights="DEFAULT").eval()
    x = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])(im).unsqueeze(0)
    with torch.no_grad():
        out = model(x)["out"][0]
    prob = torch.softmax(out, dim=0)[PERSON_CLASS].numpy()

    # Keep only the LARGEST connected person blob: the meme frame has out-of-focus foliage that the
    # model occasionally scores as person-ish, and a stray 200px island in the corner would survive
    # the crop and float next to him.
    lab, n = ndimage.label(prob > 0.5)
    if n > 1:
        sizes = ndimage.sum(np.ones_like(lab), lab, range(1, n + 1))
        keep = int(np.argmax(sizes)) + 1
        prob = np.where(lab == keep, prob, 0.0)
        print(f"  {n} person blobs, kept the largest ({int(sizes.max())} px)")

    a = np.clip((prob - 0.35) / 0.30, 0, 1)      # ramp the soft boundary into a matte
    a = ndimage.gaussian_filter(a, 0.6)          # hide the 8x-upsampled stairstep in the logits

    # Drop everything not attached to HIM. Filtering on `prob > 0.5` above is not enough: the ramp
    # starts at 0.35, so faint matte in the 0.35-0.5 band survives anywhere in the frame. It is
    # invisible to the eye but not to `getbbox()` — a few near-zero pixels at the far left inflated
    # the crop by ~300px of empty space, which then made the figure composite small.
    # Threshold at the same level the crop later treats as "present" (alpha 8), so the soft halo
    # stays attached to him and anything else is a separate component. Applied UNCONDITIONALLY: when
    # this only ran for n>1 it silently did nothing here, because a single alpha-10 speck at x=0 sat
    # below the threshold, wasn't a component at all, and still dragged the bbox 371px left — 300px of
    # transparent padding that made him composite small. Invisible on a magenta check; obvious in the
    # numbers.
    lab2, n2 = ndimage.label(a > 0.03)
    if n2 >= 1:
        sizes = ndimage.sum(np.ones_like(lab2), lab2, range(1, n2 + 1))
        a = np.where(lab2 == int(np.argmax(sizes)) + 1, a, 0.0)
        if n2 > 1:
            print(f"  dropped {n2 - 1} matte region(s) not connected to the subject")
    return (a * 255).astype(np.uint8)


def bust_crop(alpha: np.ndarray) -> tuple:
    """(left, top, right, bottom) trimming the figure to a HEAD-AND-SHOULDERS bust.

    Not cosmetic. `_add_pointing_meme` sizes the figure by HEIGHT and then caps its WIDTH, and the
    caption gets only 74% of whatever gutter is left beside it — so a wide figure is punished twice:
    it hits the width cap (rendering shorter than asked, i.e. a small face) AND squeezes the bubble.
    The full frame here is 1.51 aspect, the widest character in the set, which put his face at
    thumbnail size next to a four-line bubble of five-character lines. Cropping to the bust takes the
    aspect near 1.0, so the same height_frac spends its pixels on his FACE instead of his jacket.

    The shoulder line is measured, not guessed: the narrowest row through the neck/collar, then the
    first row below it that widens by >15%.
    """
    h, w = alpha.shape
    solid = alpha > 8
    widths = solid.sum(axis=1)
    neck_zone = slice(int(h * 0.40), int(h * 0.72))
    neck_y = int(np.argmin(widths[neck_zone]) + neck_zone.start)
    neck_w = int(widths[neck_y])
    below = np.where(widths[neck_y:] > neck_w * 1.15)[0]
    shoulder_y = int(below[0] + neck_y) if len(below) else h
    bottom = min(h, int(shoulder_y * 1.12))        # a little shoulder, so he is not a floating head
    xs = np.where(solid[:bottom].any(axis=0))[0]
    print(f"  neck row {neck_y} ({neck_w}px), shoulders {shoulder_y}, bust bottom {bottom}")
    return int(xs.min()), 0, int(xs.max()) + 1, bottom


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/nodont_kym.jpg"
    im = Image.open(src).convert("RGB")
    print(f"source {src} {im.size}")
    alpha = person_alpha(im)
    print(f"  matte covers {(alpha > 8).mean()*100:.1f}% of the frame")

    rgba = np.dstack([np.asarray(im).astype(np.uint8), alpha])
    ys, xs = np.where(alpha > 8)
    if not len(ys):
        print("no person found", file=sys.stderr)
        return 1
    l, t, r, b = bust_crop(alpha)
    out = Image.fromarray(rgba[t:b, l:r], "RGBA")
    print(f"  bust {out.size} (aspect {out.size[0]/out.size[1]:.2f}) from full {im.size}")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.save(OUT)
    print(f"wrote {OUT} {out.size}")

    chk = Image.new("RGB", out.size, (255, 0, 255))
    chk.paste(out, (0, 0), out)
    chk.save("/tmp/nodont_on_magenta.png")
    print("magenta check: /tmp/nodont_on_magenta.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
