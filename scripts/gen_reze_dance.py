"""Regenerate assets/reze_dance.mov — two anime chibi dancers (Makima + Reze), transparent bg.
700x520 RGBA, 24 frames @12fps, looping dance."""
import math, os
from PIL import Image, ImageDraw

W, H, NFRAMES = 700, 520, 24
OUT = "/tmp/reze_out"
os.makedirs(OUT, exist_ok=True)

SKIN = (250, 226, 204, 255)
LINE = (74, 52, 44, 255)
BLUSH = (247, 168, 165, 150)

def ell(d, cx, cy, rx, ry, fill, outline=LINE, w=3):
    d.ellipse([cx-rx, cy-ry, cx+rx, cy+ry], fill=fill, outline=outline, width=w)

def anime_eye(d, cx, cy, kind, hairshade):
    ew, eh = 19, 27
    box = [cx-ew, cy-eh+4, cx+ew, cy+eh]
    d.ellipse(box, fill=(255,255,255,255))            # sclera
    ir = [cx-16, cy-20, cx+16, cy+24]                  # tall iris
    if kind == "makima":
        d.ellipse(ir, fill=(243,205,70,255))
        d.chord(ir, 180, 360, fill=(196,120,34,255))   # darker top
        for r, c in [(14,(190,110,30,255)),(11,(247,210,80,255)),(8,(190,110,30,255)),(5,(247,210,80,255))]:
            d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=c, width=2)   # swirl rings
    else:
        d.ellipse(ir, fill=(126,206,150,255))
        d.chord(ir, 180, 360, fill=(46,134,92,255))     # darker top
    d.ellipse([cx-6, cy-10, cx+6, cy+12], fill=(34,24,22,255))         # pupil
    d.ellipse([cx-11, cy-17, cx-2, cy-6], fill=(255,255,255,240))      # big highlight
    d.ellipse([cx+4, cy+7, cx+10, cy+13], fill=(255,255,255,170))      # small highlight
    d.arc([cx-ew, cy-eh+2, cx+ew, cy+eh], 184, 356, fill=(38,26,28,255), width=5)  # upper lash
    d.line([cx-ew+1, cy-6, cx-ew-6, cy-12], fill=(38,26,28,255), width=4)          # outer lash
    d.arc([cx-13, cy-36, cx+13, cy-20], 200, 340, fill=hairshade, width=3)         # eyebrow

def draw_char(img, cx, base_y, bob, arm, kind):
    d = ImageDraw.Draw(img)
    cy = base_y + bob
    if kind == "makima":
        hair = (208, 46, 40, 255); hi = (240, 110, 95, 255); hsh = (150, 30, 28, 255)
    else:
        hair = (74, 44, 96, 255); hi = (120, 86, 150, 255); hsh = (48, 28, 64, 255)   # dark purple

    # back hair
    ell(d, cx, cy+8, 62, 66, hair, hair, 0)
    if kind == "makima":
        d.ellipse([cx+30, cy+12, cx+72, cy+150], fill=hair)            # long side tail
        d.ellipse([cx+40, cy+20, cx+58, cy+120], fill=hi)              # tail highlight

    ell(d, cx, cy, 55, 57, SKIN)                                       # head

    # spiky anime bangs (triangular tufts) + highlight band
    n = 7
    for i in range(n):
        x0 = cx - 52 + i*(104/n)
        x1 = x0 + (104/n)
        tipx = (x0+x1)/2
        d.polygon([(x0, cy-18),(x1, cy-18),(tipx, cy+14)], fill=hair)
    d.pieslice([cx-56, cy-64, cx+56, cy-8], 180, 360, fill=hair)        # crown
    d.arc([cx-40, cy-50, cx+40, cy-6], 200, 320, fill=hi, width=4)      # gloss highlight

    anime_eye(d, cx-23, cy+6, kind, hsh)
    anime_eye(d, cx+23, cy+6, kind, hsh)
    ell(d, cx-36, cy+26, 8, 5, BLUSH, None, 0)
    ell(d, cx+36, cy+26, 8, 5, BLUSH, None, 0)
    d.arc([cx-8, cy+26, cx+8, cy+40], 25, 155, fill=LINE, width=3)      # small mouth

    top = cy + 57
    if kind == "makima":
        d.rounded_rectangle([cx-30, top, cx+30, top+78], radius=12, fill=(246,246,250,255), outline=LINE, width=3)
        # collar V + necktie (white shirt + dark tie)
        tie = (54, 46, 60, 255)
        d.polygon([(cx-12, top),(cx, top+14),(cx+12, top)], fill=(252,252,255,255), outline=LINE)
        d.polygon([(cx-6, top+4),(cx+6, top+4),(cx+4, top+14),(cx-4, top+14)], fill=tie, outline=LINE)   # knot
        d.polygon([(cx-5, top+14),(cx+5, top+14),(cx+7, top+58),(cx, top+66),(cx-7, top+58)], fill=tie, outline=LINE)  # blade
        for lx in (cx-13, cx+13):
            d.rounded_rectangle([lx-7, top+78, lx+7, top+128], radius=5, fill=(46,46,54,255), outline=LINE, width=2)
    else:
        black = (32, 32, 38, 255)
        # WHITE sleeveless shirt (anime-accurate) + black bow tie + black skirt
        d.rounded_rectangle([cx-26, top, cx+26, top+46], radius=10, fill=(247,247,250,255), outline=LINE, width=3)
        d.polygon([(cx-17, top+3),(cx-2, top+12),(cx-17, top+21)], fill=black, outline=LINE)
        d.polygon([(cx+17, top+3),(cx+2, top+12),(cx+17, top+21)], fill=black, outline=LINE)
        d.ellipse([cx-4, top+8, cx+4, top+16], fill=black, outline=LINE)
        d.polygon([(cx-22, top+44),(cx+22, top+44),(cx+35, top+88),(cx-35, top+88)], fill=black, outline=LINE)
        for lx in (cx-13, cx+13):
            d.rounded_rectangle([lx-6, top+88, lx+6, top+126], radius=5, fill=SKIN, outline=LINE, width=2)
            d.ellipse([lx-8, top+122, lx+8, top+134], fill=(40,40,46,255), outline=LINE, width=2)

    sh_y = top + 14
    for side in (-1, 1):
        sx = cx + side*30
        ax = sx + side*30
        ay = sh_y - int(arm * side * 28) - 6
        d.line([sx, sh_y, ax, ay], fill=SKIN[:3]+(255,), width=12)
        d.ellipse([ax-8, ay-8, ax+8, ay+8], fill=SKIN, outline=LINE, width=2)

def render():
    for f in range(NFRAMES):
        ph = 2*math.pi*f/NFRAMES
        img = Image.new("RGBA", (W, H), (0,0,0,0))
        draw_char(img, 250, 330, int(9*math.sin(ph)),        0.5+0.5*math.sin(ph),        "makima")
        draw_char(img, 460, 330, int(9*math.sin(ph+math.pi)), 0.5+0.5*math.sin(ph+math.pi), "reze")
        img.save(f"{OUT}/f{f:02d}.png")
    print(f"rendered {NFRAMES} frames")

if __name__ == "__main__":
    render()

# Encode the rendered frames straight to the overlay asset (prores 4444 + alpha):
#   venv-unified/bin/python scripts/gen_reze_dance.py && \
#   ffmpeg -y -framerate 12 -i /tmp/reze_out/f%02d.png -c:v prores_ks -profile:v 4444 \
#          -pix_fmt yuva444p10le assets/reze_dance.mov
