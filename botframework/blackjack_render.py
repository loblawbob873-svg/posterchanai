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


def _card(d, x, y, card):
    """Draw one card at (x,y). card=None → face-down."""
    box = [x, y, x + CARD_W, y + CARD_H]
    if card is None:
        _rrect(d, box, 12, fill=BACK, outline=MAGENTA, width=3)
        d.text((x + CARD_W / 2, y + CARD_H / 2), "?", font=_font(54), fill=MAGENTA, anchor="mm")
        return
    rank, suit = card[:-1], card[-1]
    sym = _SUIT.get(suit, suit)
    col = RED if suit in ("H", "D") else WHITE
    _rrect(d, box, 12, fill=CARD_BG, outline=CYAN, width=3)
    d.text((x + 12, y + 8), rank, font=_font(34), fill=col, anchor="la")
    d.text((x + CARD_W / 2, y + CARD_H / 2 + 6), sym, font=_font(50), fill=col, anchor="mm")
    d.text((x + CARD_W - 12, y + CARD_H - 8), rank, font=_font(34), fill=col, anchor="rd")


def _row(d, cards, y, label, value, accent):
    n = len(cards)
    total = n * CARD_W + (n - 1) * GAP
    x0 = (W - total) // 2
    for i, c in enumerate(cards):
        _card(d, x0 + i * (CARD_W + GAP), y, c)
    d.text((24, y + CARD_H / 2), label, font=_font(26), fill=accent, anchor="lm")
    if value is not None:
        d.text((W - 24, y + CARD_H / 2), str(value), font=_font(40), fill=accent, anchor="rm")


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
