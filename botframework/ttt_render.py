"""Cyberpunk Tic-Tac-Toe board renderer (#tictactoe bot). PNG via Pillow only — no font/SVG deps,
portable to the Arc/ROCm bot nodes (mirrors chess_render). Cells are numbered 1-9 (top-left→bottom-
right) so a player can move with just the cell number."""
import io
from PIL import Image, ImageDraw, ImageFilter

BG = (11, 1, 24, 255)
GRID = (0, 240, 255, 255)        # cyan neon grid
X_NEON = (60, 230, 255, 255)     # X = cyan
O_NEON = (255, 60, 210, 255)     # O = magenta
NUM = (120, 90, 170, 255)        # faint cell numbers
LASTMOVE = (120, 255, 120, 70)
TITLE = (0, 240, 255, 255)
SUBTITLE = (200, 170, 255, 255)

CELL = 170
MARGIN = 40
TOPBAR = 76

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


def _cell_box(i, x0, y0):
    """Pixel box of cell index i (0..8, row-major) → (cx0, cy0)."""
    r, c = divmod(i, 3)
    return x0 + c * CELL, y0 + r * CELL


def render_board(cells, last_move=None, title="", subtitle="") -> bytes:
    """cells = list of 9 chars: 'X', 'O', or '' (empty). last_move = cell index 0..8 to highlight."""
    x0, y0 = MARGIN, TOPBAR
    W = MARGIN + 3 * CELL + MARGIN
    H = TOPBAR + 3 * CELL + MARGIN
    img = Image.new("RGBA", (W, H), BG)
    base = ImageDraw.Draw(img)
    # last-move wash
    if last_move is not None and 0 <= last_move < 9:
        cx, cy = _cell_box(last_move, x0, y0)
        base.rectangle([cx, cy, cx + CELL, cy + CELL], fill=LASTMOVE)

    neon = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    nd = ImageDraw.Draw(neon)
    # grid lines (inner)
    for i in (1, 2):
        nd.line([x0 + i * CELL, y0, x0 + i * CELL, y0 + 3 * CELL], fill=GRID, width=5)
        nd.line([x0, y0 + i * CELL, x0 + 3 * CELL, y0 + i * CELL], fill=GRID, width=5)
    nd.rectangle([x0 - 3, y0 - 3, x0 + 3 * CELL + 3, y0 + 3 * CELL + 3], outline=GRID, width=4)

    # faint cell numbers (only on empty cells) + marks
    nf = _font(34)
    bigf = _font(120)
    for i in range(9):
        cx, cy = _cell_box(i, x0, y0)
        mark = cells[i] if i < len(cells) else ""
        if not mark:
            nd.text((cx + 10, cy + 6), str(i + 1), font=nf, fill=NUM)
        else:
            col = X_NEON if mark == "X" else O_NEON
            tb = nd.textbbox((0, 0), mark, font=bigf)
            nd.text((cx + (CELL - (tb[2] - tb[0])) / 2 - tb[0],
                     cy + (CELL - (tb[3] - tb[1])) / 2 - tb[1]), mark, font=bigf, fill=col)

    glow = neon.filter(ImageFilter.GaussianBlur(6))
    img.alpha_composite(glow)
    img.alpha_composite(glow)
    img.alpha_composite(neon)

    td = ImageDraw.Draw(img)
    if title:
        td.text((MARGIN, 16), title, font=_font(30), fill=TITLE)
    if subtitle:
        td.text((MARGIN, 48), subtitle, font=_font(19), fill=SUBTITLE)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()
