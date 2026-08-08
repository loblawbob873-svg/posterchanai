"""Auto-split from the original effects_service.py monolith. No behavior change."""
from ._common import List, OutputFile, Path, Tuple, _BLOOD_COLORS, _CUM_COLORS, _DILDO_COLORS, _FIRE_ANIM_FPS, _FIRE_ANIM_FRAMES, _FIRE_ANIM_LOOPS, _POO_COLORS, _alive_or_still, _effects_animate, _gradient_cylinder, _gradient_sphere, _human_size, _scatter_overlay, _shade, io, is_image, logger

def _make_dildo(h: int):
    """Render one shaded, semi-anatomical dildo (pointing up) on a transparent tile.

    Pure Pillow — sphere-lit balls + glans, a cylinder-shaded shaft, a flared
    corona, urethral slit, veins and base ambient occlusion, finished with a single
    clean outer outline. Ships no image asset (the meme path is also pure Pillow).
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter, ImageChops

    W = max(int(h * 0.66), 16)
    H = max(int(h * 1.18), 20)
    base = random.choice(_DILDO_COLORS)[:3]
    outline = _shade(base, 0.45)

    cx = W / 2.0
    sw = W * 0.40                       # shaft width
    ball_r = sw * 0.62
    top = H * 0.04
    base_y = H - ball_r * 1.0
    head_h = sw * 1.16                  # glans (bell head)
    head_w = sw * 1.24
    shaft_top = top + head_h * 0.5

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # --- balls (sphere-shaded, reused for both) ---
    ball_sphere = _gradient_sphere(_shade(base, 0.95)[:3])
    bd = max(int(ball_r * 2), 2)
    bimg = ball_sphere.resize((bd, bd))
    for dx in (-sw * 0.40, sw * 0.40):
        bx = cx + dx
        tile.alpha_composite(bimg, (int(bx - ball_r), int(base_y - ball_r)))

    # --- shaft (cylinder gradient clipped to a rounded-pill mask) ---
    sh_h = max(int(base_y - shaft_top), 2)
    sh_w = max(int(sw), 2)
    cyl = _gradient_cylinder(sh_w, sh_h, base).convert("RGBA")
    smask = Image.new("L", (sh_w, sh_h), 0)
    ImageDraw.Draw(smask).rounded_rectangle([0, 0, sh_w - 1, sh_h - 1],
                                            radius=int(sw / 2), fill=255)
    cyl.putalpha(smask)
    tile.alpha_composite(cyl, (int(cx - sw / 2), int(shaft_top)))

    # --- glans (sphere-shaded, slightly pinker, squashed into a bell) ---
    pink = (min(255, base[0] + 20), max(0, base[1] - 6), min(255, base[2] + 8))
    glans = _gradient_sphere(pink).resize((max(int(head_w), 2), max(int(head_h), 2)))
    tile.alpha_composite(glans, (int(cx - head_w / 2), int(top)))

    # Silhouette of the solid body so far — confines the soft passes below.
    sil = tile.split()[-1]

    # --- ambient occlusion where the shaft meets the balls ---
    ao = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ao).ellipse([cx - sw * 0.75, base_y - ball_r * 0.5,
                                cx + sw * 0.75, base_y + ball_r * 0.6],
                               fill=(0, 0, 0, 95))
    ao = ao.filter(ImageFilter.GaussianBlur(max(sw * 0.16, 1)))
    ao.putalpha(ImageChops.multiply(ao.split()[-1], sil))
    tile.alpha_composite(ao)

    # --- veins (two soft wavy lines down the shaft) ---
    veins = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vd = ImageDraw.Draw(veins)
    vcol = _shade(base, 0.8)[:3] + (95,)
    for vx, ph in ((cx - sw * 0.16, 0.0), (cx + sw * 0.1, 1.7)):
        pts = []
        for i in range(9):
            yy = shaft_top + sh_h * i / 8.0
            xx = vx + math.sin(i * 0.9 + ph) * sw * 0.12
            pts.append((xx, yy))
        vd.line(pts, fill=vcol, width=max(int(sw * 0.05), 1), joint="curve")
    veins = veins.filter(ImageFilter.GaussianBlur(max(sw * 0.03, 0.6)))
    veins.putalpha(ImageChops.multiply(veins.split()[-1], sil))
    tile.alpha_composite(veins)

    # --- corona (flared rim) + urethral slit ---
    fd = ImageDraw.Draw(tile)
    fd.arc([cx - head_w / 2, top + head_h * 0.42, cx + head_w / 2, top + head_h * 1.28],
           start=18, end=162, fill=_shade(base, 0.55)[:3] + (160,),
           width=max(int(sw * 0.07), 1))
    slit_y = top + head_h * 0.16
    fd.line([(cx, slit_y), (cx, slit_y + head_h * 0.18)],
            fill=_shade(base, 0.38)[:3] + (190,), width=max(int(sw * 0.05), 1))

    # --- single clean outer outline (edge of the union silhouette) ---
    ow = max(int(W * 0.03), 1)
    binr = tile.split()[-1].point(lambda a: 255 if a > 40 else 0)
    eroded = binr.filter(ImageFilter.MinFilter(ow * 2 + 1))
    edge = ImageChops.subtract(binr, eroded)
    line_layer = Image.new("RGBA", (W, H), outline[:3] + (255,))
    tile.paste(line_layer, (0, 0), edge)

    return tile


def add_dildos(data: bytes, count: int = 0) -> bytes:
    """Scatter cartoon dildos at random positions/sizes/angles over an image.

    `count` <= 0 auto-scales with the image area. Returns JPEG bytes.
    """
    return _scatter_overlay(data, _make_dildo, count)


def dildo_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Scatter dildos over the first image attachment.

    Returns (output_files, summary_text). Mirrors meme_attachments so the web UI,
    Telegram and the fedi bots share one delivery path.
    """
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."

    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_dildos(data)
        out = _alive_or_still(result, stem, "dildo")
        summary = f"## 🍆 Dildo\n\n🍆 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"dildo failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _make_poo(h: int):
    """Render one realistic coiled stool on a transparent tile (pure Pillow).

    A tapering stack of DISTINCT sphere-shaded coils swaying side-to-side up to a
    pinched tip, finished with dark grooves between coils, surface speckle
    (texture), base ambient occlusion, and a few moist specular highlights. No
    cartoon face — aims for a believable turd. Ships no image asset (like dildo).
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter, ImageChops

    W = max(int(h * 0.95), 16)
    H = max(int(h * 1.12), 18)
    base = random.choice(_POO_COLORS)[:3]
    cx = W / 2.0
    phase = random.uniform(0, math.tau)

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # A handful of DISTINCT coils (not a smooth cone): each a wide sphere-shaded
    # bulge, stacked bottom→top with ~45% overlap so the seams read as separate
    # soft-serve rings, tapering and swaying side-to-side toward a pinched tip.
    n = 5
    base_w = W * 0.96
    coils = []  # (cx, cy, w, h) per bulge, low→high
    for i in range(n):
        t = i / (n - 1)
        w = base_w * (1 - 0.60 * t)
        hgt = w * 0.66
        cy = H * 0.84 - t * (H * 0.60)
        seg_cx = cx + math.sin(i * 1.7 + phase) * W * 0.13 * (1 - 0.5 * t)
        shade = 0.82 + 0.30 * (1 - t)
        seg = _gradient_sphere(_shade(base, shade)[:3]).resize(
            (max(int(w), 2), max(int(hgt), 2)))
        tile.alpha_composite(seg, (int(seg_cx - w / 2), int(cy - hgt / 2)))
        coils.append((seg_cx, cy, w, hgt))

    # Pinched tip — a small narrow bulge crowning the top coil.
    tcx, tcy, tw, th = coils[-1]
    tip = _gradient_sphere(_shade(base, 1.18)[:3]).resize(
        (max(int(tw * 0.42), 2), max(int(th * 0.95), 2)))
    tile.alpha_composite(tip, (int(tcx - tw * 0.21), int(tcy - th * 0.85)))

    sil = tile.split()[-1]  # silhouette — confines every soft pass below

    # --- grooves: a soft dark band where each coil tucks under the one above ---
    grv = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grv)
    for (lx, ly, lw, lh), (ux, uy, uw, uh) in zip(coils, coils[1:]):
        gy = (ly - lh / 2 + uy + uh / 2) / 2  # seam between the two bulges
        gw = min(lw, uw) * 0.5
        gx = (lx + ux) / 2
        gd.ellipse([gx - gw, gy - lh * 0.14, gx + gw, gy + lh * 0.14],
                   fill=(0, 0, 0, 130))
    grv = grv.filter(ImageFilter.GaussianBlur(max(W * 0.03, 1)))
    grv.putalpha(ImageChops.multiply(grv.split()[-1], sil))
    tile.alpha_composite(grv)

    # --- surface speckle (subtle lighter/darker flecks for matte texture) ---
    spk = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(spk)
    for _ in range(max((W * H) // 240, 24)):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        rr = random.uniform(0.6, 1.9)
        c = _shade(base, random.uniform(0.55, 1.4))[:3] + (random.randint(35, 85),)
        sd.ellipse([x - rr, y - rr, x + rr, y + rr], fill=c)
    spk = spk.filter(ImageFilter.GaussianBlur(0.5))
    spk.putalpha(ImageChops.multiply(spk.split()[-1], sil))
    tile.alpha_composite(spk)

    # --- ambient occlusion pooled where it meets the ground ---
    bx, by, bw, bh = coils[0]
    ao = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ao).ellipse(
        [bx - bw * 0.55, by + bh * 0.1, bx + bw * 0.55, by + bh * 0.6],
        fill=(0, 0, 0, 120))
    ao = ao.filter(ImageFilter.GaussianBlur(max(bw * 0.10, 1)))
    ao.putalpha(ImageChops.multiply(ao.split()[-1], sil))
    tile.alpha_composite(ao)

    # --- moist specular highlights (a small soft sheen upper-left of each coil) ---
    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    for seg_cx, cy, w, hgt in coils:
        hx, hy = seg_cx - w * 0.26, cy - hgt * 0.30
        hrx, hry = w * 0.10, hgt * 0.075
        hd.ellipse([hx - hrx, hy - hry, hx + hrx, hy + hry],
                   fill=(255, 250, 238, 95))
    hl = hl.filter(ImageFilter.GaussianBlur(max(W * 0.015, 0.6)))
    hl.putalpha(ImageChops.multiply(hl.split()[-1], sil))
    tile.alpha_composite(hl)

    return tile


def add_poo(data: bytes, count: int = 0) -> bytes:
    """Scatter realistic piles of poop at random positions/sizes over an image.

    `count` <= 0 auto-scales with the image area. The spin is kept small so each
    coil stays roughly upright. Returns JPEG bytes.
    """
    return _scatter_overlay(data, _make_poo, count, max_rotation=22.0)


def poo_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Scatter poop over the first image attachment.

    Returns (output_files, summary_text). Mirrors dildo_attachments so the web UI,
    Telegram and the fedi bots share one delivery path.
    """
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."

    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_poo(data)
        out = _alive_or_still(result, stem, "poo")
        summary = f"## 💩 Poo\n\n💩 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"poo failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _make_cum(h: int, rng=None, grow: float = 1.0):
    """Render one glossy off-white splatter (the "cum" gag) on a transparent tile.

    Pure Pillow — an irregular central blob plus a few radiating strands tipped
    with droplets (and the odd satellite speck), given a soft translucent dark rim
    so the near-white body still reads on light backgrounds, plus wet specular
    highlights and slight translucency. Ships no image asset (like the poo path).

    For animation: pass a seeded ``rng`` (a ``random.Random``) so the blob's shape is
    stable frame-to-frame, and a ``grow`` in [0,1] that scales how far the flung
    strands have shot out — advance it across frames and the strands ooze outward.
    """
    import math
    import random as _rnd
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    random = rng if rng is not None else _rnd

    W = max(int(h * 1.15), 18)
    H = max(int(h * 1.15), 18)
    base = random.choice(_CUM_COLORS)[:3]
    cx, cy = W * 0.5, H * 0.52
    phase = random.uniform(0, math.tau)

    # --- build the splatter SHAPE on an alpha mask (lets us rim/shade it after) ---
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    main_r = W * 0.19

    def _dot(x, y, r):
        md.ellipse([x - r, y - r, x + r, y + r], fill=255)

    # A guaranteed-solid core so the blob never has an interior pinhole (a gap
    # would let the outer-rim pass leak inward as a dark ring).
    md.ellipse([cx - main_r, cy - main_r * 0.85, cx + main_r, cy + main_r * 0.85], fill=255)

    # Cohesive central blob: several big, tightly-overlapping ellipses (lots of
    # overlap so there are no interior gaps that would shade into dark artifacts).
    for _ in range(5):
        ox = cx + random.uniform(-1, 1) * main_r * 0.35
        oy = cy + random.uniform(-1, 1) * main_r * 0.30
        rx = main_r * random.uniform(0.8, 1.15)
        ry = main_r * random.uniform(0.7, 1.0)
        md.ellipse([ox - rx, oy - ry, ox + rx, oy + ry], fill=255)

    # Flung streaks: a tapered tail (wide at the blob, thinning out) capped by a
    # fatter droplet head — reads like fluid thrown outward, not a molecule graph.
    for i in range(random.randint(4, 6)):
        ang = phase + i * (math.tau / 5) + random.uniform(-0.4, 0.4)
        dist = main_r * random.uniform(1.4, 3.0) * grow
        dx, dy = math.cos(ang), math.sin(ang)
        steps = 12
        for s in range(steps + 1):
            f = s / steps
            px = cx + dx * (main_r * 0.5 + f * dist)
            py = cy + dy * (main_r * 0.5 + f * dist)
            rad = main_r * (0.26 * (1 - f) ** 1.4 + 0.04)
            _dot(px, py, rad)
        # droplet head at the tip, slightly past the tail end
        hx, hy = cx + dx * (main_r * 0.5 + dist), cy + dy * (main_r * 0.5 + dist)
        _dot(hx, hy, main_r * random.uniform(0.16, 0.30))
        # an occasional small satellite fleck beyond the head
        if random.random() < 0.5:
            _dot(hx + dx * main_r * 0.7, hy + dy * main_r * 0.7,
                 main_r * random.uniform(0.06, 0.13))

    # Morphological close (dilate→erode) to seal any thin gaps between strokes,
    # then a light blur for soft edges.
    _k = max(int(W * 0.02) | 1, 3)
    mask = mask.filter(ImageFilter.MaxFilter(_k)).filter(ImageFilter.MinFilter(_k))
    sil = mask.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.7)))  # soft edges

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # --- soft translucent dark rim just outside the shape (so white reads on white) ---
    grow = max(int(W * 0.03) | 1, 3)
    ring = ImageChops.subtract(sil.filter(ImageFilter.MaxFilter(grow)), sil)
    ring = ring.filter(ImageFilter.GaussianBlur(max(W * 0.02, 1)))
    rim = Image.new("RGBA", (W, H), (50, 50, 60, 0))
    rim.putalpha(ring.point(lambda a: int(a * 0.55)))
    tile.alpha_composite(rim)

    # --- body fill (slightly translucent for a wet look) ---
    body = Image.new("RGBA", (W, H), base + (236,))
    body.putalpha(ImageChops.multiply(body.split()[-1], sil))
    tile.alpha_composite(body)

    # --- inner edge shading (darker cream rim) for a little volume ---
    inner = ImageChops.subtract(sil, sil.filter(ImageFilter.MinFilter(grow)))
    inner = inner.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
    shade = Image.new("RGBA", (W, H), _shade(base, 0.82)[:3] + (0,))
    shade.putalpha(ImageChops.multiply(inner, sil).point(lambda a: int(a * 0.33)))
    tile.alpha_composite(shade)

    # --- wet specular highlights (a few bright spots on the blob) ---
    # Draw + blur on an ALPHA mask, then tint a uniformly-white layer with it: if
    # we blurred a coloured RGBA layer instead, its transparent (black) RGB would
    # bleed into a dark halo — very visible on a near-white body.
    hlmask = Image.new("L", (W, H), 0)
    hd = ImageDraw.Draw(hlmask)
    for _ in range(3):
        hx = cx + random.uniform(-main_r * 0.5, main_r * 0.3)
        hy = cy + random.uniform(-main_r * 0.5, main_r * 0.1)
        hr = main_r * random.uniform(0.12, 0.26)
        hd.ellipse([hx - hr, hy - hr * 0.7, hx + hr, hy + hr * 0.7], fill=235)
    hlmask = hlmask.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
    hl = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    hl.putalpha(ImageChops.multiply(hlmask, sil))
    tile.alpha_composite(hl)

    return tile


def add_cum(data: bytes, count: int = 0) -> bytes:
    """Scatter glossy off-white splatters at random positions/sizes/angles over an image.

    `count` <= 0 auto-scales with the image area. Returns JPEG bytes.
    """
    return _scatter_overlay(data, _make_cum, count)


def cum_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Scatter a full glossy splatter over the first image attachment.

    Always a still JPEG (the full, fully-grown splatter) — NOT the animated MP4, even when effect
    animation is enabled. Mirrors poo_attachments so the web UI, Telegram and the fedi bots
    share one delivery path.
    """
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."

    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_cum(data)   # full splatter, fully-grown (grow=1.0); still image, no video
        out: OutputFile = {
            "filename": f"{stem}_cum.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 💦 Cum\n\n💦 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"cum failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _make_blood(h: int, rng=None, grow: float = 1.0):
    """Render one wet blood SPLATTER on a transparent tile (pure Pillow).

    This is impact spatter, not movie blood: a cohesive central blob with flung
    tapering strands tipped by droplet heads, jagged surface-tension fingers around
    the rim, and a fine secondary spray. Same geometry as the cum splatter (they are
    both a fluid hitting something at speed) — what differs is the material: blood
    colours, a near-opaque body, a darker pooled inner edge and a small wet
    highlight.

    It used to be the OTHER thing: a pool sitting high on a tall tile with long
    gravity drips running down it, which is the horror-movie look and reads as
    painted-on rather than splattered. Nothing needs the tall tile now, so it is
    square-ish like the cum one, and the caller no longer has to pin rotation to
    keep drips pointing down.

    For animation: pass a seeded ``rng`` (a ``random.Random``) so the splatter's
    shape is stable frame-to-frame, and a ``grow`` in [0,1] that scales how far the
    flung strands have shot out — advance it across frames and the splatter throws.
    """
    import math
    import random as _rnd
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    random = rng if rng is not None else _rnd

    W = max(int(h * 1.5), 24)
    H = max(int(h * 1.5), 24)
    base = random.choice(_BLOOD_COLORS)[:3]
    cx, cy = W * 0.5, H * 0.5
    main_r = W * 0.15
    phase = random.uniform(0, math.tau)

    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)

    def _dot(x, y, r):
        md.ellipse([x - r, y - r, x + r, y + r], fill=255)

    def _trail(x0, y0, x1, y1, wa, wb):
        """A smooth tapering trail from (x0,y0,width wa) to (x1,y1,width wb):
        overlapping dots spaced finer than their radius so it reads continuous."""
        seg = math.hypot(x1 - x0, y1 - y0)
        n = max(int(seg / max(min(wa, wb) * 0.5, 1.0)), 6)
        for s in range(n + 1):
            f = s / n
            _dot(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, max(wa + (wb - wa) * f, 1.0))

    # Guaranteed-solid core first: an interior pinhole would let the outer-rim pass
    # leak inward as a dark ring (the same trap the cum blob documents).
    md.ellipse([cx - main_r, cy - main_r * 0.88, cx + main_r, cy + main_r * 0.88], fill=255)

    # Directional, irregular pool: globs spread ALONG a random impact axis (so it's
    # elongated, not a round blob) with heavy overlap so there are no interior gaps.
    axis = random.uniform(0, math.tau)
    ax, ay = math.cos(axis), math.sin(axis)
    perpx, perpy = -ay, ax
    for _ in range(9):
        t = random.uniform(-1.0, 1.0)            # along the impact axis
        s = random.uniform(-0.4, 0.4)            # small perpendicular jitter
        ox = cx + ax * t * main_r * 1.05 + perpx * s * main_r
        oy = cy + ay * t * main_r * 1.05 + perpy * s * main_r
        _dot(ox, oy, main_r * random.uniform(0.45, 0.75))

    # Pointed rim fingers: the surface-tension spikes that make it read as a splat
    # rather than a ball of paint.
    for _ in range(random.randint(8, 13)):
        a = random.uniform(0, math.tau)
        r0 = main_r * random.uniform(0.5, 0.95)
        fl = main_r * random.uniform(0.35, 1.2)
        sx, sy = cx + math.cos(a) * r0, cy + math.sin(a) * r0
        ex, ey = cx + math.cos(a) * (r0 + fl), cy + math.sin(a) * (r0 + fl)
        _trail(sx, sy, ex, ey, main_r * random.uniform(0.09, 0.16), main_r * 0.02)
        if random.random() < 0.45:
            _dot(ex, ey, main_r * random.uniform(0.04, 0.09))

    # Flung strands: tapered tails capped by a fatter droplet head, thrown outward
    # in every direction. `grow` scales the throw, so the animated version spreads.
    for i in range(random.randint(5, 8)):
        ang = phase + i * (math.tau / 6) + random.uniform(-0.5, 0.5)
        dist = main_r * random.uniform(1.4, 3.1) * grow
        dx, dy = math.cos(ang), math.sin(ang)
        sx, sy = cx + dx * main_r * 0.5, cy + dy * main_r * 0.5
        ex, ey = cx + dx * (main_r * 0.5 + dist), cy + dy * (main_r * 0.5 + dist)
        _trail(sx, sy, ex, ey, main_r * random.uniform(0.12, 0.22), main_r * 0.03)
        _dot(ex, ey, main_r * random.uniform(0.09, 0.21))            # droplet head

    # Fine secondary spray, clustered along a random impact direction, plus a few
    # stray specks — the detail that sells it as spatter rather than a decal. It
    # travels with `grow` too, so early animation frames aren't a bare blob ringed
    # by droplets that were already at their final distance.
    spray = random.uniform(0, math.tau)
    for _ in range(random.randint(14, 28)):
        a = spray + random.uniform(-1.0, 1.0)
        d = main_r * random.uniform(1.0, 3.2) * grow
        _dot(cx + math.cos(a) * d, cy + math.sin(a) * d,
             main_r * random.uniform(0.03, 0.13))
    for _ in range(random.randint(4, 9)):
        a = random.uniform(0, math.tau)
        d = main_r * random.uniform(0.8, 3.0) * grow
        _dot(cx + math.cos(a) * d, cy + math.sin(a) * d,
             main_r * random.uniform(0.02, 0.07))

    sil = mask.filter(ImageFilter.GaussianBlur(max(W * 0.005, 0.5)))

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # Soft dark rim just outside the shape -> depth / separation from the photo.
    ring_px = max(int(W * 0.025) | 1, 3)
    ring = ImageChops.subtract(sil.filter(ImageFilter.MaxFilter(ring_px)), sil)
    ring = ring.filter(ImageFilter.GaussianBlur(max(W * 0.02, 1)))
    rim = Image.new("RGBA", (W, H), (20, 0, 0, 0))
    rim.putalpha(ring.point(lambda a: int(a * 0.6)))
    tile.alpha_composite(rim)

    # Body fill (nearly opaque — wet blood).
    body = Image.new("RGBA", (W, H), base + (250,))
    body.putalpha(ImageChops.multiply(body.split()[-1], sil))
    tile.alpha_composite(body)

    # Darker inner edge for a pooled, glossy look.
    inner = ImageChops.subtract(sil, sil.filter(ImageFilter.MinFilter(ring_px)))
    inner = inner.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
    shade = Image.new("RGBA", (W, H), _shade(base, 0.55)[:3] + (0,))
    shade.putalpha(ImageChops.multiply(inner, sil).point(lambda a: int(a * 0.5)))
    tile.alpha_composite(shade)

    # Wet specular highlight: one soft vertical-ish sheen on the upper-left of the
    # pool (vertical so it doesn't read like a pair of eyes), via the alpha-mask
    # method to avoid a dark blur halo. A tiny offset speck adds wetness.
    hlmask = Image.new("L", (W, H), 0)
    hd = ImageDraw.Draw(hlmask)
    hx, hy = cx - main_r * 0.30, cy - main_r * 0.28
    hd.ellipse([hx - main_r * 0.11, hy - main_r * 0.22,
                hx + main_r * 0.11, hy + main_r * 0.22], fill=200)
    hd.ellipse([cx + main_r * 0.12, cy - main_r * 0.02,
                cx + main_r * 0.20, cy + main_r * 0.06], fill=120)
    hlmask = hlmask.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
    hl = Image.new("RGBA", (W, H), (255, 235, 235, 0))
    hl.putalpha(ImageChops.multiply(hlmask, sil))
    tile.alpha_composite(hl)

    return tile


def add_blood(data: bytes, count: int = 0) -> bytes:
    """Scatter wet blood splatters over an image.

    `count` <= 0 auto-scales with the image area. Full spin: a splatter has no
    "up", unlike the dripping pool this used to draw. Returns JPEG bytes.
    """
    return _scatter_overlay(data, _make_blood, count)


def blood_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Scatter blood over the first image attachment.

    Always a still JPEG (the full, fully-grown splatter) — NOT the animated MP4, even when effect
    animation is enabled. Mirrors cum_attachments so the web UI, Telegram and the fedi bots share
    one delivery path.
    """
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."

    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_blood(data)   # full splatter, fully-grown (grow=1.0); still image, no video
        out: OutputFile = {
            "filename": f"{stem}_blood.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🩸 Blood\n\n🩸 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"blood failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _make_bullethole(h: int):
    """Render one bullet hole on a transparent tile (pure Pillow).

    A punched impact on a SOLID surface: a dark irregular penetration ringed by a
    ragged, pulverised crater of deformed material, with only a few short uneven
    stress chips and a little knocked-out debris. Deliberately NOT the dense even
    radial + concentric crack pattern that read as a spider web. Ships no image asset.
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter

    W = max(int(h * 1.4), 22)
    H = W
    cx = cy = W / 2.0
    hole_r = W * 0.15
    crater_r = hole_r * 1.55
    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    def _blob(rmin, rmax, m=20):
        """A closed irregular blob polygon of points between rmin..rmax of crater_r."""
        pts = []
        for i in range(m):
            a = (i / m) * math.tau
            rr = crater_r * random.uniform(rmin, rmax)
            pts.append((cx + math.cos(a) * rr, cy + math.sin(a) * rr))
        return pts

    # --- pulverised crater rim: a ragged lighter ring of deformed material (the
    #     surface punched out), soft-edged and irregular so it never reads as a disc. ---
    crater = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(crater).polygon(_blob(0.78, 1.18), fill=(150, 146, 143, 90))
    crater = crater.filter(ImageFilter.GaussianBlur(max(W * 0.05, 1.5)))
    tile.alpha_composite(crater)

    # --- darker bruise just inside the crater (depth toward the hole) ---
    bruise = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(bruise).polygon(_blob(0.42, 0.72), fill=(78, 74, 74, 170))
    bruise = bruise.filter(ImageFilter.GaussianBlur(max(W * 0.018, 0.8)))
    tile.alpha_composite(bruise)

    # --- a FEW short stress chips: uneven angles, no branching, no concentric rings ---
    chips = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    chd = ImageDraw.Draw(chips)
    for _ in range(random.randint(3, 6)):
        a = random.uniform(0, math.tau)
        length = W * random.uniform(0.10, 0.24)
        steps = random.randint(2, 4)
        seg = length / steps
        x, y = cx + math.cos(a) * hole_r * 0.85, cy + math.sin(a) * hole_r * 0.85
        pts, aa = [(x, y)], a
        for _ in range(steps):
            aa += random.uniform(-0.3, 0.3)
            x += math.cos(aa) * seg
            y += math.sin(aa) * seg
            pts.append((x, y))
        chd.line(pts, fill=(36, 33, 33, 210), width=max(int(W * 0.012), 1), joint="curve")
    tile.alpha_composite(chips)

    # --- knocked-out debris specks scattered just outside the crater ---
    deb = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dbd = ImageDraw.Draw(deb)
    for _ in range(random.randint(4, 9)):
        a = random.uniform(0, math.tau)
        d = random.uniform(crater_r * 0.55, crater_r * 1.35)
        rr = W * random.uniform(0.006, 0.018)
        ox, oy = cx + math.cos(a) * d, cy + math.sin(a) * d
        dbd.ellipse([ox - rr, oy - rr, ox + rr, oy + rr],
                    fill=(46, 43, 43, random.randint(110, 200)))
    tile.alpha_composite(deb)

    # --- the hole itself: a dark irregular penetration with a faint torn rim ---
    hole = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hole)
    pts = []
    for i in range(12):
        a = (i / 12) * math.tau
        r = hole_r * random.uniform(0.7, 1.25)
        pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
    hd.polygon(pts, fill=(10, 9, 11, 255))
    hd.line(pts + [pts[0]], fill=(72, 66, 60, 170), width=max(int(W * 0.01), 1), joint="curve")
    hole = hole.filter(ImageFilter.GaussianBlur(0.5))
    tile.alpha_composite(hole)

    return tile


def add_bulletholes(data: bytes, count: int = 0) -> bytes:
    """Punch scattered bullet holes over an image. `count` <= 0 auto-scales. JPEG bytes."""
    return _scatter_overlay(data, _make_bullethole, count)


def bullethole_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Punch bullet holes into the first image attachment. Mirrors blood_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_bulletholes(data)
        out = _alive_or_still(result, stem, "bulletholes")
        summary = f"## 🕳️ Bullet holes\n\n🕳️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"bullethole failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _make_fire(h: int, phase=None):
    """Render one flame on a transparent tile (pure Pillow).

    Nested flame silhouettes from dark-red → red → orange → yellow → near-white
    core (a hot gradient), each with wobbling licks toward a tapered tip, plus a
    soft outer glow. Slightly translucent for an additive look. No image asset.

    `phase` (radians) sets where the flame is in its wobble cycle — pass an
    advancing value to animate the licks frame-to-frame; defaults to random (a
    fixed still flame).
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter, ImageChops

    W = max(int(h * 0.95), 18)
    H = max(int(h * 1.35), 26)
    cxf = W * 0.5
    base_y = H * 0.92
    if phase is None:
        phase = random.uniform(0, math.tau)
    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    def _flame_mask(scale: float, wob: float):
        m = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(m)
        fw = W * 0.40 * scale          # half-width at the base
        fh = H * 0.84 * scale          # height
        n = 22
        left, right = [], []
        for i in range(n + 1):
            f = i / n
            y = base_y - f * fh
            # taper to the tip, with sinusoidal licks that grow toward the top
            w = fw * (1 - f) ** 0.65 * (1 + wob * 0.5 * math.sin(f * 7 + phase))
            sway = math.sin(f * 3.0 + phase) * fw * 0.16 * f
            left.append((cxf - w + sway, y))
            right.append((cxf + w + sway, y))
        d.polygon(left + list(reversed(right)), fill=255)
        d.ellipse([cxf - fw, base_y - fw * 0.5, cxf + fw, base_y + fw * 0.45], fill=255)
        return m

    # Outer glow (dark-red, blurred) then the hot nested layers. Same phase so the
    # licks of each layer line up and read as one flame with a bright core.
    layers = [
        (1.00, (120, 18, 4), 0.9, True),    # dark red glow
        (0.94, (210, 40, 6), 0.9, False),   # red
        (0.74, (255, 130, 18), 0.7, False), # orange
        (0.52, (255, 205, 60), 0.5, False), # yellow
        (0.30, (255, 248, 210), 0.35, False),  # white-hot core
    ]
    for scale, col, wob, glow in layers:
        m = _flame_mask(scale, wob)
        if glow:
            m = m.filter(ImageFilter.GaussianBlur(max(W * 0.06, 1)))
            alpha = 150
        else:
            m = m.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
            alpha = 235
        lyr = Image.new("RGBA", (W, H), col + (0,))
        lyr.putalpha(m.point(lambda a, _al=alpha: int(a * _al / 255)))
        tile.alpha_composite(lyr)

    return tile


def add_fire(data: bytes, count: int = 0) -> bytes:
    """Set the image alight: a wall of flames across the bottom third.

    Rather than scattering flames everywhere, this builds a continuous row of
    overlapping flames of varying heights rooted at the bottom edge (rising up to
    ~a third of the image, taller licks higher), over a warm glow rising from the
    bottom. Returns JPEG bytes.
    """
    import random
    from PIL import Image, ImageOps, ImageDraw, ImageFilter
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        img = img.convert("RGBA")
        band = int(H * 0.34)                       # flames fill the lower third

        # Warm glow rising from the bottom edge (alpha gradient → tinted layer).
        gmask = Image.new("L", (1, H), 0)
        for y in range(H):
            if y >= H - band:
                f = (y - (H - band)) / band
                gmask.putpixel((0, y), int(140 * (f ** 1.5)))
        glow = Image.new("RGBA", (W, H), (255, 95, 15, 0))
        glow.putalpha(gmask.resize((W, H)))
        img.alpha_composite(glow)

        # Wall of flames: march across the width with overlap, random heights.
        x = -int(W * 0.04)
        while x < W:
            fh = int(band * random.uniform(0.78, 1.45))   # some licks exceed the band
            size = max(int(fh / 1.35), 14)
            flame = _make_fire(size)
            if random.random() < 0.5:
                flame = flame.transpose(Image.FLIP_LEFT_RIGHT)
            # root the flame's base at the image bottom (slight sink so no gap shows)
            y = H - flame.height + int(flame.height * 0.04)
            img.alpha_composite(flame, (x, y))
            x += int(flame.width * random.uniform(0.42, 0.66))

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def add_fire_animated(data: bytes) -> bytes:
    """Set the image alight as a looping MP4 — the flames actually flicker.

    Same wall-of-flames layout as `add_fire`, but the per-flame positions/sizes are
    fixed once and each flame's wobble `phase` advances over a wrapping cycle, so the
    licks dance frame-to-frame. Returns silent H.264 MP4 bytes (routed like the audio
    gags). Falls back to the still `add_fire` JPEG only via the caller on error.
    """
    import math
    import random
    from PIL import Image, ImageOps
    from app.services.media_service import frames_to_video
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        base = img.convert("RGBA")
        band = int(H * 0.34)

        # Warm glow rising from the bottom edge — static across frames.
        gmask = Image.new("L", (1, H), 0)
        for y in range(H):
            if y >= H - band:
                f = (y - (H - band)) / band
                gmask.putpixel((0, y), int(140 * (f ** 1.5)))
        glow = Image.new("RGBA", (W, H), (255, 95, 15, 0))
        glow.putalpha(gmask.resize((W, H)))
        base.alpha_composite(glow)

        # Fix the flame layout ONCE (position, size, flip, phase offset) so only the
        # licks move between frames — re-randomising per frame would just boil noise.
        flames = []
        x = -int(W * 0.04)
        while x < W:
            fh = int(band * random.uniform(0.78, 1.45))
            size = max(int(fh / 1.35), 14)
            flip = random.random() < 0.5
            ph0 = random.uniform(0, math.tau)
            flames.append((x, size, flip, ph0))
            x += int(size * 0.95 * random.uniform(0.42, 0.66)) or 1

        frames = []
        for fi in range(_FIRE_ANIM_FRAMES):
            t = fi / _FIRE_ANIM_FRAMES          # 0 → just-under-1, wraps to 0
            frame = base.copy()
            for fx, size, flip, ph0 in flames:
                flame = _make_fire(size, phase=ph0 + math.tau * t)
                if flip:
                    flame = flame.transpose(Image.FLIP_LEFT_RIGHT)
                y = H - flame.height + int(flame.height * 0.04)
                frame.alpha_composite(flame, (fx, y))
            frames.append(frame.convert("RGB"))

    return frames_to_video(frames, fps=_FIRE_ANIM_FPS, loops=_FIRE_ANIM_LOOPS)


def fire_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Set the first image attachment on fire. Mirrors blood_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        if _effects_animate():
            try:
                result = add_fire_animated(data)
                out: OutputFile = {
                    "filename": f"{stem}_fire.mp4",
                    "data": result,
                    "content_type": "video/mp4",
                }
                summary = f"## 🔥 Fire\n\n🔥 {filename}: {_human_size(len(result))}"
                return [out], summary
            except Exception as e:
                logger.warning(f"animated fire failed for {filename}, using still: {e}")
        result = add_fire(data)
        out = {
            "filename": f"{stem}_fire.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🔥 Fire\n\n🔥 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"fire failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"
