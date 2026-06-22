"""Cyberpunk Blackjack table renderer (#blackjack bot). Pillow-only PNG (no external deps).
Draws the dealer's hand (top) and the player's hand (bottom) as neon cards, with hand values and a
title/subtitle. A hidden dealer hole-card renders face-down until the hand is over."""
import io
from PIL import Image, ImageDraw

BG = (11, 1, 24, 255)
CYAN = (60, 230, 255, 255)
MAGENTA = (255, 60, 210, 255)
CARD_BG = (22, 10, 40, 255)
RED = (255, 80, 110, 255)        # hearts / diamonds
WHITE = (235, 240, 255, 255)
TITLE = (0, 240, 255, 255)
SUB = (200, 170, 255, 255)
BACK = (40, 18, 70, 255)         # face-down card

W, H = 680, 460
CARD_W, CARD_H, GAP = 92, 128, 16

_SUIT = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
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


def _rrect(d, box, radius, **kw):
    try:
        d.rounded_rectangle(box, radius=radius, **kw)
    except Exception:
        d.rectangle(box, **kw)


def _card(d, x, y, card, w=CARD_W, h=CARD_H):
    """Draw one card at (x,y), sized w×h. card=None → face-down. Fonts scale with the card."""
    box = [x, y, x + w, y + h]
    rf, sf, pad = max(12, int(h * 0.27)), max(16, int(h * 0.40)), max(5, int(w * 0.10))
    if card is None:
        _rrect(d, box, 10, fill=BACK, outline=MAGENTA, width=3)
        d.text((x + w / 2, y + h / 2), "?", font=_font(sf), fill=MAGENTA, anchor="mm")
        return
    rank, suit = card[:-1], card[-1]
    sym = _SUIT.get(suit, suit)
    col = RED if suit in ("H", "D") else WHITE
    _rrect(d, box, 10, fill=CARD_BG, outline=CYAN, width=3)
    d.text((x + pad, y + pad - 2), rank, font=_font(rf), fill=col, anchor="la")
    d.text((x + w / 2, y + h / 2 + 4), sym, font=_font(sf), fill=col, anchor="mm")
    d.text((x + w - pad, y + h - pad + 2), rank, font=_font(rf), fill=col, anchor="rd")


def _row(d, cards, y, label, value, accent, cw=CARD_W, ch=CARD_H, gap=GAP, outcome=None):
    n = max(1, len(cards))
    total = n * cw + (n - 1) * gap
    x0 = (W - total) // 2
    for i, c in enumerate(cards):
        _card(d, x0 + i * (cw + gap), y, c, cw, ch)
    extra = f"  {outcome}" if outcome else ""
    d.text((24, y + ch / 2), label + extra, font=_font(max(16, int(ch * 0.30))), fill=accent, anchor="lm")
    if value is not None:
        d.text((W - 24, y + ch / 2), str(value), font=_font(max(20, int(ch * 0.42))), fill=accent, anchor="rm")


_OUT_LABEL = {"win": "WON", "blackjack": "BLACKJACK", "lose": "LOST", "push": "PUSH"}


def render_table(dealer, dealer_value, seats, hide_hole=True, title="", subtitle="") -> bytes:
    """Multi-seat board: dealer row on top, then each seat. seats = [(name, hand, value, outcome)]."""
    cw, ch, gap = 56, 78, 10
    rowh = ch + 30
    h2 = 96 + (1 + len(seats)) * rowh
    img = Image.new("RGBA", (W, h2), BG)
    d = ImageDraw.Draw(img)
    if title:
        d.text((W / 2, 28), title, font=_font(28), fill=TITLE, anchor="mm")
    if subtitle:
        d.text((W / 2, 60), subtitle[:84], font=_font(16), fill=SUB, anchor="mm")
    y = 92
    dcards = [c if not (hide_hole and i == 1) else None for i, c in enumerate(dealer)]
    _row(d, dcards, y, "DEALER", (None if hide_hole else dealer_value), MAGENTA, cw, ch, gap)
    y += rowh
    for (name, hand, val, outcome) in seats:
        _row(d, hand, y, (name or "?")[:14], val, CYAN, cw, ch, gap, outcome=_OUT_LABEL.get(outcome))
        y += rowh
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def render(dealer, player, dealer_value, player_value, hide_hole=True, title="", subtitle="") -> bytes:
    """dealer/player = list of card strings ('TS','AH',…). hide_hole → dealer's 2nd card face-down
    and dealer_value shown as None (the up-card only counts publicly)."""
    img = Image.new("RGBA", (W, H), BG)
    d = ImageDraw.Draw(img)
    if title:
        d.text((W / 2, 28), title, font=_font(30), fill=TITLE, anchor="mm")
    if subtitle:
        d.text((W / 2, 60), subtitle, font=_font(20), fill=SUB, anchor="mm")
    dealer_cards = [c if not (hide_hole and i == 1) else None for i, c in enumerate(dealer)]
    _row(d, dealer_cards, 90, "DEALER", (None if hide_hole else dealer_value), MAGENTA)
    _row(d, player, 285, "YOU", player_value, CYAN)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()
