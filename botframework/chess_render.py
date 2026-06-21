"""Cyberpunk chess-board renderer for the #chesstr bot.

Renders a `python-chess` position to a neon PNG (no external font/SVG deps — pieces are drawn as
vector silhouettes with a glow, so it's portable to the Arc/ROCm bot nodes). The side to move has
its pieces NUMBERED so a player can move with the simple "<n> <square>" syntax (e.g. "1 d4").

Public API:
  piece_numbers(board, color) -> {int: square}   stable numbering of `color`'s pieces (a1..h8 order)
  render_board(fen, ...) -> PNG bytes
"""
import io

import chess
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---- cyberpunk palette ----
BG = (11, 1, 24, 255)            # near-black violet (matches the client theme)
SQ_LIGHT = (38, 18, 64, 255)     # dark purple
SQ_DARK = (18, 8, 38, 255)       # darker
GRID = (0, 240, 255, 255)        # cyan neon
WHITE_NEON = (60, 230, 255, 255)  # white pieces = cyan
BLACK_NEON = (255, 60, 210, 255)  # black pieces = magenta
LASTMOVE = (120, 255, 120, 90)   # green wash on the last move's squares
LABEL = (120, 200, 230, 255)
NUM_FILL = (255, 224, 0, 255)    # numbering badge
NUM_TEXT = (10, 6, 20, 255)
TITLE = (0, 240, 255, 255)
SUBTITLE = (200, 170, 255, 255)

CELL = 84
MARGIN = 46
TOPBAR = 72

_FONT_CANDIDATES = [
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(sz: int):
    from PIL import ImageFont as _IF
    for p in _FONT_CANDIDATES:
        try:
            return _IF.truetype(p, sz)
        except Exception:
            continue
    try:
        return _IF.load_default(sz)
    except Exception:
        return _IF.load_default()


def piece_numbers(board: chess.Board, color: bool) -> dict:
    """Number `color`'s pieces 1..N in a stable a1→h8 square order (same order the renderer draws)."""
    out, n = {}, 0
    for sq in chess.SQUARES:           # 0 (a1) .. 63 (h8)
        pc = board.piece_at(sq)
        if pc and pc.color == color:
            n += 1
            out[n] = sq
    return out


def _cell_xy(sq: int, x0: int, y0: int):
    """Top-left pixel of a square's cell (white's perspective: a1 bottom-left)."""
    f = chess.square_file(sq)
    r = chess.square_rank(sq)
    return x0 + f * CELL, y0 + (7 - r) * CELL


# ---- piece silhouettes, defined in a unit cell [0,1]x[0,1] (y down) ----
def _poly(draw, box, pts, color):
    x0, y0, w = box
    draw.polygon([(x0 + px * w, y0 + py * w) for (px, py) in pts], fill=color)


def _ellipse(draw, box, cx, cy, rx, ry, color):
    x0, y0, w = box
    draw.ellipse([x0 + (cx - rx) * w, y0 + (cy - ry) * w, x0 + (cx + rx) * w, y0 + (cy + ry) * w], fill=color)


def _rect(draw, box, a, b, c, d, color):
    x0, y0, w = box
    draw.rectangle([x0 + a * w, y0 + b * w, x0 + c * w, y0 + d * w], fill=color)


def _base(draw, box, color):
    _poly(draw, box, [(0.24, 0.90), (0.30, 0.80), (0.70, 0.80), (0.76, 0.90)], color)
    _rect(draw, box, 0.22, 0.88, 0.78, 0.93, color)


def _draw_pawn(d, box, c):
    _base(d, box, c)
    _poly(d, box, [(0.38, 0.44), (0.62, 0.44), (0.70, 0.82), (0.30, 0.82)], c)
    _ellipse(d, box, 0.5, 0.32, 0.14, 0.14, c)


def _draw_rook(d, box, c):
    _base(d, box, c)
    _poly(d, box, [(0.30, 0.46), (0.70, 0.46), (0.74, 0.82), (0.26, 0.82)], c)
    _rect(d, box, 0.27, 0.34, 0.73, 0.46, c)
    for a, b in ((0.27, 0.37), (0.46, 0.56), (0.65, 0.75)):
        _rect(d, box, a, 0.22, b, 0.36, c)


def _draw_knight(d, box, c):
    _base(d, box, c)
    _poly(d, box, [
        (0.32, 0.84), (0.32, 0.58), (0.27, 0.50), (0.33, 0.40), (0.30, 0.33),
        (0.40, 0.20), (0.52, 0.14), (0.49, 0.25), (0.62, 0.22), (0.73, 0.34),
        (0.75, 0.54), (0.68, 0.72), (0.66, 0.84),
    ], c)


def _draw_bishop(d, box, c):
    _base(d, box, c)
    _poly(d, box, [(0.40, 0.52), (0.60, 0.52), (0.67, 0.82), (0.33, 0.82)], c)
    _ellipse(d, box, 0.5, 0.40, 0.15, 0.18, c)
    _ellipse(d, box, 0.5, 0.20, 0.06, 0.06, c)


def _draw_queen(d, box, c):
    _base(d, box, c)
    _poly(d, box, [(0.30, 0.52), (0.70, 0.52), (0.74, 0.82), (0.26, 0.82)], c)
    _poly(d, box, [
        (0.28, 0.50), (0.32, 0.26), (0.41, 0.44), (0.50, 0.22),
        (0.59, 0.44), (0.68, 0.26), (0.72, 0.50),
    ], c)
    for px in (0.32, 0.50, 0.68):
        _ellipse(d, box, px, 0.24, 0.05, 0.05, c)


def _draw_king(d, box, c):
    _base(d, box, c)
    _poly(d, box, [(0.32, 0.50), (0.68, 0.50), (0.72, 0.82), (0.28, 0.82)], c)
    _rect(d, box, 0.30, 0.40, 0.70, 0.52, c)
    _rect(d, box, 0.46, 0.12, 0.54, 0.34, c)   # cross vertical
    _rect(d, box, 0.39, 0.18, 0.61, 0.26, c)   # cross horizontal


_DRAW = {
    chess.PAWN: _draw_pawn, chess.ROOK: _draw_rook, chess.KNIGHT: _draw_knight,
    chess.BISHOP: _draw_bishop, chess.QUEEN: _draw_queen, chess.KING: _draw_king,
}


def render_board(fen: str, last_move=None, number_color=None,
                 title: str = "", subtitle: str = "") -> bytes:
    """Render `fen` to neon PNG bytes. last_move=(from_sq,to_sq) highlights it; number_color (a
    chess color, or None) overlays move-numbers on that side's pieces."""
    board = chess.Board(fen)
    x0, y0 = MARGIN, TOPBAR
    W = MARGIN + 8 * CELL + MARGIN
    H = TOPBAR + 8 * CELL + MARGIN

    img = Image.new("RGBA", (W, H), BG)
    base = ImageDraw.Draw(img)
    # squares
    for sq in chess.SQUARES:
        cx, cy = _cell_xy(sq, x0, y0)
        light = (chess.square_file(sq) + chess.square_rank(sq)) % 2 == 1
        base.rectangle([cx, cy, cx + CELL, cy + CELL], fill=SQ_LIGHT if light else SQ_DARK)
    # last-move wash
    if last_move:
        wash = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        wd = ImageDraw.Draw(wash)
        for sq in last_move:
            if sq is None:
                continue
            cx, cy = _cell_xy(sq, x0, y0)
            wd.rectangle([cx, cy, cx + CELL, cy + CELL], fill=LASTMOVE)
        img.alpha_composite(wash)

    # neon layer (pieces + grid + labels) — gets a glow pass
    neon = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    nd = ImageDraw.Draw(neon)
    # grid + outer frame
    for i in range(9):
        nd.line([x0 + i * CELL, y0, x0 + i * CELL, y0 + 8 * CELL], fill=GRID, width=2)
        nd.line([x0, y0 + i * CELL, x0 + 8 * CELL, y0 + i * CELL], fill=GRID, width=2)
    nd.rectangle([x0 - 3, y0 - 3, x0 + 8 * CELL + 3, y0 + 8 * CELL + 3], outline=GRID, width=4)
    # coordinate labels
    lf = _font(22)
    for f in range(8):
        ch = chr(ord('a') + f)
        nd.text((x0 + f * CELL + CELL / 2 - 6, y0 + 8 * CELL + 10), ch, font=lf, fill=LABEL)
    for r in range(8):
        nd.text((x0 - 26, y0 + (7 - r) * CELL + CELL / 2 - 12), str(r + 1), font=lf, fill=LABEL)
    # pieces
    pad = 0.06
    for sq in chess.SQUARES:
        pc = board.piece_at(sq)
        if not pc:
            continue
        cx, cy = _cell_xy(sq, x0, y0)
        box = (cx + pad * CELL, cy + pad * CELL, CELL * (1 - 2 * pad))
        col = WHITE_NEON if pc.color == chess.WHITE else BLACK_NEON
        _DRAW[pc.piece_type](nd, box, col)

    # glow: blur the neon layer underneath, then the crisp neon on top
    glow = neon.filter(ImageFilter.GaussianBlur(7))
    img.alpha_composite(glow)
    img.alpha_composite(glow)
    img.alpha_composite(neon)

    # number badges for the side to move (crisp, on top)
    if number_color is not None:
        nums = piece_numbers(board, number_color)
        nf = _font(26)
        top = ImageDraw.Draw(img)
        for n, sq in nums.items():
            cx, cy = _cell_xy(sq, x0, y0)
            bx, by, r = cx + 15, cy + 13, 15
            top.ellipse([bx - r, by - r, bx + r, by + r], fill=NUM_FILL, outline=(20, 12, 30, 255), width=2)
            s = str(n)
            tb = top.textbbox((0, 0), s, font=nf)
            top.text((bx - (tb[2] - tb[0]) / 2, by - (tb[3] - tb[1]) / 2 - tb[1]), s, font=nf, fill=NUM_TEXT)

    # title bar
    td = ImageDraw.Draw(img)
    if title:
        td.text((MARGIN, 16), title, font=_font(30), fill=TITLE)
    if subtitle:
        td.text((MARGIN, 46), subtitle, font=_font(20), fill=SUBTITLE)
    td.text((W - 140, 18), "#chesstr", font=_font(22), fill=BLACK_NEON)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()
