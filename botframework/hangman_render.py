"""Cyberpunk Hangman renderer (#hangman bot). Pillow-only PNG (no font/SVG deps). Draws a neon
gallows that builds up with each wrong guess, the masked word, and the wrong letters."""
import io
from PIL import Image, ImageDraw, ImageFilter

BG = (11, 1, 24, 255)
NEON = (0, 240, 255, 255)        # gallows = cyan
FIG = (255, 60, 210, 255)        # the figure = magenta (danger)
WORD = (60, 230, 255, 255)
WRONG = (255, 80, 90, 255)
TITLE = (0, 240, 255, 255)
SUBTITLE = (200, 170, 255, 255)
MISS = (150, 110, 180, 255)

W, H = 620, 600
MAX_WRONG = 6

_FONTS = ["/usr/share/fonts/liberation-fonts/LiberationMono-Bold.ttf",
          "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
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


def render(display, wrong_letters, wrong_count, title="", subtitle="") -> bytes:
    """display = string like '_ A _ _ E _' ; wrong_letters = list of missed letters ; wrong_count int."""
    img = Image.new("RGBA", (W, H), BG)
    neon = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    nd = ImageDraw.Draw(neon)

    # gallows (left side) — cyan
    gx, gy = 70, 110
    nd.line([(gx, gy + 320), (gx + 160, gy + 320)], fill=NEON, width=6)   # base
    nd.line([(gx + 40, gy + 320), (gx + 40, gy)], fill=NEON, width=6)      # post
    nd.line([(gx + 40, gy), (gx + 150, gy)], fill=NEON, width=6)          # beam
    nd.line([(gx + 150, gy), (gx + 150, gy + 40)], fill=NEON, width=5)    # rope

    # the figure (magenta) — one part per wrong guess
    hx, hy = gx + 150, gy + 70
    parts = wrong_count
    if parts >= 1:
        nd.ellipse([hx - 26, hy - 26, hx + 26, hy + 26], outline=FIG, width=5)            # head
    if parts >= 2:
        nd.line([(hx, hy + 26), (hx, hy + 130)], fill=FIG, width=5)                       # torso
    if parts >= 3:
        nd.line([(hx, hy + 50), (hx - 45, hy + 90)], fill=FIG, width=5)                   # left arm
    if parts >= 4:
        nd.line([(hx, hy + 50), (hx + 45, hy + 90)], fill=FIG, width=5)                   # right arm
    if parts >= 5:
        nd.line([(hx, hy + 130), (hx - 40, hy + 190)], fill=FIG, width=5)                 # left leg
    if parts >= 6:
        nd.line([(hx, hy + 130), (hx + 40, hy + 190)], fill=FIG, width=5)                 # right leg

    glow = neon.filter(ImageFilter.GaussianBlur(6))
    img.alpha_composite(glow); img.alpha_composite(neon)

    d = ImageDraw.Draw(img)
    if title:
        d.text((30, 18), title, font=_font(30), fill=TITLE)
    if subtitle:
        d.text((30, 52), subtitle, font=_font(19), fill=SUBTITLE)

    # masked word — big, but AUTO-FIT to the available width so long words (e.g. "watermelon")
    # aren't clipped off the right edge of the image.
    word_x = 250
    max_w = W - word_x - 16
    wf = _font(46)
    for _sz in range(46, 15, -2):
        wf = _font(_sz)
        try:
            _w = d.textlength(display, font=wf)
        except Exception:
            _bb = d.textbbox((0, 0), display, font=wf); _w = _bb[2] - _bb[0]
        if _w <= max_w:
            break
    d.text((word_x, 180), display, font=wf, fill=WORD)
    # remaining guesses
    d.text((300, 250), f"Misses: {wrong_count}/{MAX_WRONG}", font=_font(24), fill=MISS)
    # wrong letters
    if wrong_letters:
        d.text((300, 290), "Wrong: " + " ".join(wrong_letters), font=_font(26), fill=WRONG)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()
