"""Cyberpunk Connect Four renderer (#connect4 bot). Pillow-only PNG (no font/SVG deps). 7×6 grid,
discs glow cyan (player 1) / magenta (player 2); columns numbered 1-7 above the board."""
import io
from PIL import Image, ImageDraw, ImageFilter

BG = (11, 1, 24, 255)
FRAME = (0, 240, 255, 255)        # cyan neon board frame/holes
HOLE = (16, 8, 36, 255)           # empty hole fill
P1 = (60, 230, 255, 255)          # player 1 = cyan
P2 = (255, 60, 210, 255)          # player 2 = magenta
LABEL = (150, 110, 180, 255)
TITLE = (0, 240, 255, 255)
SUBTITLE = (200, 170, 255, 255)
LASTRING = (120, 255, 120, 255)

COLS, ROWS = 7, 6
CELL = 78
MARGIN = 30
TOPBAR = 78
PAD = 9          # gap between discs

_FONTS = ["/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
          "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
          "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]


def _font(sz):
    from PIL import ImageFont
    for p in _FONTS:
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            continue
    try:
        return ImageFont.load_default(sz)
    except Exception:
        return ImageFont.load_default()


def render(cells, last_move=None, title="", subtitle="") -> bytes:
    """cells = 42 entries (row 0 = TOP), '', '1', or '2'. last_move = index 0..41 to ring."""
    x0, y0 = MARGIN, TOPBAR
    W = MARGIN + COLS * CELL + MARGIN
    H = TOPBAR + ROWS * CELL + MARGIN + 24
    img = Image.new("RGBA", (W, H), BG)

    # neon frame + holes glow layer
    neon = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    nd = ImageDraw.Draw(neon)
    nd.rounded_rectangle([x0 - 6, y0 - 6, x0 + COLS * CELL + 6, y0 + ROWS * CELL + 6],
                         radius=18, outline=FRAME, width=4)
    base = ImageDraw.Draw(img)
    for r in range(ROWS):
        for c in range(COLS):
            cx = x0 + c * CELL + CELL // 2
            cy = y0 + r * CELL + CELL // 2
            rad = CELL // 2 - PAD
            v = cells[r * COLS + c] if r * COLS + c < len(cells) else ""
            if v == "1":
                col = P1
            elif v == "2":
                col = P2
            else:
                col = None
            # empty hole on the base image; discs on the neon (glow) layer
            base.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=HOLE, outline=(40, 24, 70, 255), width=2)
            if col:
                nd.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=col)

    glow = neon.filter(ImageFilter.GaussianBlur(6))
    img.alpha_composite(glow)
    img.alpha_composite(neon)

    top = ImageDraw.Draw(img)
    # column numbers
    cf = _font(26)
    for c in range(COLS):
        s = str(c + 1)
        tb = top.textbbox((0, 0), s, font=cf)
        top.text((x0 + c * CELL + (CELL - (tb[2] - tb[0])) / 2 - tb[0], y0 + ROWS * CELL + 6), s, font=cf, fill=LABEL)
    # last-move ring
    if last_move is not None and 0 <= last_move < ROWS * COLS:
        r, c = divmod(last_move, COLS)
        cx = x0 + c * CELL + CELL // 2
        cy = y0 + r * CELL + CELL // 2
        rad = CELL // 2 - PAD
        top.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=LASTRING, width=4)

    if title:
        top.text((MARGIN, 16), title, font=_font(30), fill=TITLE)
    if subtitle:
        top.text((MARGIN, 48), subtitle, font=_font(19), fill=SUBTITLE)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()
