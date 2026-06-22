"""Cyberpunk Texas Hold'em renderer (#holdem bot). Pillow-only PNG, neon-on-dark to match the rest
of the games. Cards are ints 0..51 (see holdem_engine). Two views:
  * render_table(state, reveal) — the public felt: community board + pot + every seat (stack, bet,
    dealer button, who's to act, folded/all-in). reveal=True shows hole cards at showdown.
  * render_seat(state, pk)      — a player's private DM view: their hole cards big + the board + pot.
"""
import io
from PIL import Image, ImageDraw, ImageFont
from holdem_engine import RANKS, SUITS, rank_of, suit_of

BG = (10, 2, 22, 255)
FELT = (14, 30, 26, 255)         # poker felt, dark teal
FELT_EDGE = (0, 240, 255, 255)
CYAN = (60, 230, 255, 255)
MAGENTA = (255, 60, 210, 255)
GOLD = (255, 210, 90, 255)
CARD_BG = (24, 12, 44, 255)
RED = (255, 80, 110, 255)        # ♥ ♦
WHITE = (235, 240, 255, 255)
DIM = (120, 110, 150, 255)
TITLE = (0, 240, 255, 255)
SUB = (200, 170, 255, 255)
BACK = (44, 20, 78, 255)         # face-down

_FONTS = ["/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
          "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
          "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]


def _font(sz):
    for p in _FONTS:
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            continue
    try:
        return ImageFont.load_default(sz)
    except Exception:
        return ImageFont.load_default()


def _rrect(d, box, r, **kw):
    try:
        d.rounded_rectangle(box, radius=r, **kw)
    except Exception:
        d.rectangle(box, **kw)


def _card(d, x, y, card, w, h, face_down=False, empty=False):
    box = [x, y, x + w, y + h]
    if empty:
        # an undealt community slot — a quiet outlined placeholder, NOT a "?" (which reads as a
        # hidden card). Keeps the board legible at pre-flop/flop/turn.
        _rrect(d, box, 9, outline=(70, 64, 92, 255), width=2)
        return
    if card is None or face_down:
        _rrect(d, box, 9, fill=BACK, outline=MAGENTA, width=3)
        d.text((x + w / 2, y + h / 2), "?", font=_font(int(h * 0.42)), fill=MAGENTA, anchor="mm")
        return
    rank = RANKS[rank_of(card)]
    suit = SUITS[suit_of(card)]
    col = RED if suit_of(card) in (1, 2) else WHITE
    pad = max(4, int(w * 0.11))
    _rrect(d, box, 9, fill=CARD_BG, outline=CYAN, width=3)
    d.text((x + pad, y + pad - 2), rank, font=_font(int(h * 0.28)), fill=col, anchor="la")
    d.text((x + w / 2, y + h / 2 + 3), suit, font=_font(int(h * 0.40)), fill=col, anchor="mm")
    d.text((x + w - pad, y + h - pad + 2), rank, font=_font(int(h * 0.28)), fill=col, anchor="rd")


def _bg(w, h):
    img = Image.new("RGBA", (w, h), BG)
    d = ImageDraw.Draw(img)
    # subtle neon grid
    for gx in range(0, w, 40):
        d.line([(gx, 0), (gx, h)], fill=(22, 8, 40, 255), width=1)
    for gy in range(0, h, 40):
        d.line([(0, gy), (w, gy)], fill=(22, 8, 40, 255), width=1)
    return img, d


def render_table(state, reveal=False):
    seats = state["seats"]
    names = state.get("names", {})
    board = state.get("board", [])
    pot = sum(state.get("contrib", {}).values())
    W = 720
    H = 250 + 86 * ((len(seats) + 1) // 2)
    img, d = _bg(W, H)
    # felt ellipse
    _rrect(d, [30, 70, W - 30, 210], 70, fill=FELT, outline=FELT_EDGE, width=3)
    d.text((W / 2, 22), "♠ TEXAS HOLD'EM ♥", font=_font(30), fill=TITLE, anchor="mm")
    # community board (5 slots; face-down for undealt)
    cw, ch, gap = 70, 98, 12
    total = 5 * cw + 4 * gap
    bx = (W - total) // 2
    for i in range(5):
        card = board[i] if i < len(board) else None
        _card(d, bx + i * (cw + gap), 92, card, cw, ch, empty=(card is None))
    d.text((W / 2, 200), f"POT  {pot}", font=_font(26), fill=GOLD, anchor="mm")
    if state.get("status") == "done" and state.get("result"):
        d.text((W / 2, 232), state["result"][:70], font=_font(17), fill=MAGENTA, anchor="mm")
    elif state.get("to_act"):
        d.text((W / 2, 232), f"Action on {names.get(state['to_act'], '?')}",
                font=_font(18), fill=CYAN, anchor="mm")
    # seats grid below
    folded = set(state.get("folded", []))
    allin = set(state.get("allin", []))
    winners = state.get("winners", {})
    y0 = 260
    for idx, pk in enumerate(seats):
        col = idx % 2
        row = idx // 2
        x = 40 + col * (W // 2 - 20)
        y = y0 + row * 86
        bw = W // 2 - 60
        is_turn = state.get("to_act") == pk
        won = winners.get(pk, 0)
        border = GOLD if won else (CYAN if is_turn else (DIM if pk in folded else MAGENTA))
        _rrect(d, [x, y, x + bw, y + 74], 12, fill=(20, 8, 38, 255), outline=border, width=3)
        nm = names.get(pk, "@?")[:16]
        d.text((x + 12, y + 8), nm, font=_font(18),
               fill=(DIM if pk in folded else WHITE), anchor="la")
        # dealer button: a small gold "D" disc (the 🔘 emoji isn't in the bundled fonts → tofu box)
        if seats.index(pk) == state.get("button", -1):
            try:
                nmw = d.textlength(nm, font=_font(18))
            except Exception:
                nmw = len(nm) * 10
            cx, cy = x + 12 + nmw + 16, y + 16
            d.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], fill=GOLD, outline=WHITE, width=2)
            d.text((cx, cy), "D", font=_font(15), fill=(20, 8, 38, 255), anchor="mm")
        status = ("FOLDED" if pk in folded else ("ALL-IN" if pk in allin else
                  (f"bet {state.get('street_bet', {}).get(pk, 0)}" if state.get('street_bet', {}).get(pk) else "")))
        d.text((x + 12, y + 38), f"stack {state.get('stacks', {}).get(pk, 0)}   {status}",
               font=_font(15), fill=(GOLD if won else SUB), anchor="la")
        # reveal hole cards at showdown for non-folded players
        if reveal and pk not in folded and pk in state.get("hole", {}):
            hc = state["hole"][pk]
            for j, c in enumerate(hc[:2]):
                _card(d, x + bw - 44 - (1 - j) * 38, y + 6, c, 34, 48)
        if won:
            d.text((x + bw - 12, y + 8), f"+{won}", font=_font(18), fill=GOLD, anchor="ra")
    return _png(img)


def render_seat(state, pk):
    """Private DM view for one player: big hole cards + the board + pot/to-call."""
    W, H = 560, 360
    img, d = _bg(W, H)
    d.text((W / 2, 24), "YOUR HAND", font=_font(28), fill=TITLE, anchor="mm")
    hole = state.get("hole", {}).get(pk, [])
    cw, ch, gap = 120, 168, 24
    tot = 2 * cw + gap
    bx = (W - tot) // 2
    for i, c in enumerate(hole[:2]):
        _card(d, bx + i * (cw + gap), 56, c, cw, ch)
    # board strip
    board = state.get("board", [])
    d.text((W / 2, 246), "BOARD", font=_font(16), fill=SUB, anchor="mm")
    bcw, bch, bgap = 56, 78, 8
    btot = 5 * bcw + 4 * bgap
    bbx = (W - btot) // 2
    for i in range(5):
        card = board[i] if i < len(board) else None
        _card(d, bbx + i * (bcw + bgap), 262, card, bcw, bch, empty=(card is None))
    pot = sum(state.get("contrib", {}).values())
    call = max(0, state.get("to_call", 0) - state.get("street_bet", {}).get(pk, 0))
    d.text((W / 2, H - 12), f"POT {pot}   ·   TO CALL {call}   ·   STACK {state.get('stacks', {}).get(pk, 0)}",
           font=_font(18), fill=GOLD, anchor="mm")
    return _png(img)


def _png(img):
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()
