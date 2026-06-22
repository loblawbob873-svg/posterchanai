"""Cyberpunk Blackjack renderer (#blackjack bot). Pillow-only PNG, neon-on-dark to match hold'em.
Cards are "<rank><suit>" strings (e.g. "AS","TD"); ranks A,2..9,T,J,Q,K and suits S,H,D,C. Two views:
  * render_table(state, reveal) — the public felt: dealer + every seat (hand, value, bet, result, stack).
    reveal=False hides the dealer hole card; reveal=True shows it (showdown).
  * render_seat(state, pk)      — a player's private DM view: their big hand + the dealer up card + chips.
"""
import io
from PIL import Image, ImageDraw, ImageFont

SUITS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
BG = (10, 2, 22, 255)
FELT = (14, 30, 26, 255)
FELT_EDGE = (0, 240, 255, 255)
CYAN = (60, 230, 255, 255)
MAGENTA = (255, 60, 210, 255)
GOLD = (255, 210, 90, 255)
GREEN = (93, 255, 176, 255)
CARD_BG = (24, 12, 44, 255)
RED = (255, 80, 110, 255)
WHITE = (235, 240, 255, 255)
DIM = (120, 110, 150, 255)
TITLE = (0, 240, 255, 255)

_FONTS = ["/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
          "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
          "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]
BACK = (44, 20, 78, 255)


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


def _hand_value(hand):
    total, aces = 0, 0
    for c in hand:
        r = c[:-1]
        if r == "A":
            total += 11; aces += 1
        elif r in ("T", "J", "Q", "K"):
            total += 10
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total


def _card(d, x, y, card, w, h, face_down=False):
    box = [x, y, x + w, y + h]
    if card is None or face_down:
        _rrect(d, box, 9, fill=BACK, outline=MAGENTA, width=3)
        d.text((x + w / 2, y + h / 2), "?", font=_font(int(h * 0.42)), fill=MAGENTA, anchor="mm")
        return
    rank = card[:-1]
    suit = SUITS.get(card[-1], "?")
    col = RED if card[-1] in ("H", "D") else WHITE
    pad = max(4, int(w * 0.11))
    _rrect(d, box, 9, fill=CARD_BG, outline=CYAN, width=3)
    d.text((x + pad, y + pad - 2), rank, font=_font(int(h * 0.26)), fill=col, anchor="la")
    d.text((x + w / 2, y + h / 2 + 3), suit, font=_font(int(h * 0.40)), fill=col, anchor="mm")
    d.text((x + w - pad, y + h - pad + 2), rank, font=_font(int(h * 0.26)), fill=col, anchor="rd")


def _bg(w, h):
    img = Image.new("RGBA", (w, h), BG)
    d = ImageDraw.Draw(img)
    for gx in range(0, w, 40):
        d.line([(gx, 0), (gx, h)], fill=(22, 8, 40, 255), width=1)
    for gy in range(0, h, 40):
        d.line([(0, gy), (w, gy)], fill=(22, 8, 40, 255), width=1)
    return img, d


def _chip(d, x, y, r=9):
    d.ellipse([x - r, y - r, x + r, y + r], fill=GOLD, outline=WHITE, width=2)


def _png(img):
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _dealer_cards(state, reveal):
    if state.get("dhand"):
        dh = list(state["dhand"])
        return [c if not ((not reveal) and i > 0) else None for i, c in enumerate(dh)], dh
    up = state.get("dealer_up")
    down = state.get("dealer_down", 1)
    cards = ([up] + [None] * down) if up else []
    return cards, []


def render_table(state, reveal=False):
    seats = state.get("seats", [])
    names = state.get("names", {})
    bot = state.get("bot")
    players = [p for p in seats if p != bot]
    W = 720
    H = 220 + 92 * max(1, (len(players) + 1) // 2)
    img, d = _bg(W, H)
    d.text((W / 2, 24), "♠ BLACKJACK ♥", font=_font(30), fill=TITLE, anchor="mm")
    _rrect(d, [30, 54, W - 30, 190], 26, fill=FELT, outline=FELT_EDGE, width=3)
    shown, full = _dealer_cards(state, reveal)
    lbl = "DEALER"
    if reveal and full:
        lbl = f"DEALER · {_hand_value(full)}" + (" BUST" if _hand_value(full) > 21 else "")
    d.text((W / 2, 72), lbl, font=_font(17), fill=CYAN, anchor="mm")
    cw, ch, gap = 62, 90, 10
    n = max(1, len(shown))
    bx = (W - (n * cw + (n - 1) * gap)) // 2
    for i, c in enumerate(shown):
        _card(d, bx + i * (cw + gap), 92, c, cw, ch)
    results = state.get("results", {})
    payouts = state.get("payouts", {})
    folded = set(state.get("folded", []))
    y0 = 210
    for idx, pk in enumerate(players):
        x = 40 + (idx % 2) * (W // 2 - 20)
        y = y0 + (idx // 2) * 92
        bw = W // 2 - 60
        hand = state.get("hands", {}).get(pk, [])
        pv = _hand_value(hand)
        out = results.get(pk)
        net = payouts.get(pk, 0)
        border = GREEN if out in ("win", "blackjack") else \
            (GOLD if out == "push" else (DIM if (pk in folded or out == "lose") else MAGENTA))
        _rrect(d, [x, y, x + bw, y + 80], 12, fill=(20, 8, 38, 255), outline=border, width=3)
        d.text((x + 12, y + 9), (names.get(pk, "@?") or "@?")[:15], font=_font(17), fill=WHITE, anchor="la")
        cx = x + 12
        for c in hand[:5]:
            _card(d, cx, y + 30, c, 34, 46)
            cx += 39
        d.text((x + bw - 12, y + 9), f"{pv}", font=_font(20), fill=(RED if pv > 21 else WHITE), anchor="ra")
        _chip(d, x + bw - 66, y + 40, 8)
        d.text((x + bw - 53, y + 40), f"{state.get('stacks', {}).get(pk, 0)}", font=_font(15), fill=GOLD, anchor="lm")
        if out:
            lab = {"win": f"+{net}", "blackjack": f"+{net} BJ", "push": "PUSH", "lose": f"-{abs(net)}"}.get(out, "")
            d.text((x + bw - 12, y + 64), lab, font=_font(15),
                   fill=(GREEN if net > 0 else (GOLD if out == "push" else RED)), anchor="rm")
    return _png(img)


def render_seat(state, pk):
    """Private DM view — the player's big hand + the dealer up card + a gold chips/bet bar."""
    W, H = 600, 400
    img, d = _bg(W, H)
    d.text((W / 2, 28), "♠ YOUR HAND ♥", font=_font(30), fill=TITLE, anchor="mm")
    hand = state.get("hands", {}).get(pk, [])
    pv = _hand_value(hand)
    cw, ch, gap = 104, 150, 18
    n = max(1, len(hand))
    bx = (W - (n * cw + (n - 1) * gap)) // 2
    for i, c in enumerate(hand):
        _card(d, bx + i * (cw + gap), 56, c, cw, ch)
    tail = "  ·  BUST" if pv > 21 else ("  ·  21!" if pv == 21 else "")
    d.text((W / 2, 226), f"YOU HAVE {pv}{tail}", font=_font(18), fill=(RED if pv > 21 else GOLD), anchor="mm")
    fy = 248
    _rrect(d, [22, fy, W - 22, fy + 110], 20, fill=FELT, outline=FELT_EDGE, width=3)
    d.text((W / 2, fy + 16), "DEALER SHOWS", font=_font(13), fill=CYAN, anchor="mm")
    up = (state.get("dhand") or [None])[0] if state.get("dhand") else state.get("dealer_up")
    _card(d, (W - 58) // 2, fy + 30, up, 58, 76, face_down=(up is None))
    by = H - 32
    _rrect(d, [22, by, W - 22, by + 26], 11, fill=(26, 16, 48, 255), outline=GOLD, width=2)
    _chip(d, 38, by + 13, 9)
    d.text((54, by + 13), f"{state.get('stacks', {}).get(pk, 0)}", font=_font(18), fill=GOLD, anchor="lm")
    d.text((W - 30, by + 13), f"BET {state.get('bet', {}).get(pk, 0)}", font=_font(17), fill=CYAN, anchor="rm")
    return _png(img)
