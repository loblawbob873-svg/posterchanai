"""Regenerate assets/nami_money.mov + assets/nami_money.mp3 -- the `nami` effect.

Nami (One Piece) with MONEY BAGS FOR PUPILS, rubbing her hands with a sly grin -- the gag the series itself
uses whenever she smells treasure ("can you add a cool happy merchent effect" became, at the owner's
direction, "nami with money bags for pupils, hand rubbing", her own design: an adult character in her usual
bikini top).

Same route as `uwu` (scripts/gen_uwu_dance.py): drawn by THIS NODE'S OWN anime model (`POST
/api/generate-image`, the word "anime" selects it), cut out with the rembg the `removebackground` command
uses, then ANIMATED. The model draws the pose and the grin; the money-bag pupils are painted on afterwards
(bag_eyes), because no seed ever put them in the eyes -- it drew bags BESIDE her.

**The sprite is committed (assets/nami_money_sprite.png) and reused by default.** Image generation is not
deterministic; re-running without it draws a different Nami. --redraw only when that is the point.

The motion: her clasped hands slide side to side three times a second (the rub) under a feathered mask, her
body bobs once a second, and gold coins float up past her. All periodic in a 2 s cycle, so the asset is
ONE cycle that `-stream_loop -1` repeats with no seam. The audio is synthesized here -- a cash-register
"ka-ching" -- with an optional voice line (edge-tts ja-JP-NanamiNeural, 「うふふ〜♪ お金〜！」) mixed on top.

Output: alpha ProRes 4444 (yuva444p10le), 12 fps, like every other overlay clip.

Run from the repo root:  venv-unified/bin/python scripts/gen_nami_money.py [--redraw]
"""
import base64
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.request
import wave

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PROMPT = ("anime, masterpiece, best quality, Nami from One Piece, adult woman, long orange hair, bikini top, cleavage, "
          "rubbing her hands together greedily, sly mischievous grin, money bag symbols as her pupils, "
          "upper body, cel-shaded, clean lineart, plain white background")
NEGATIVE = "lowres, bad hands, extra fingers, missing fingers, deformed, blurry, text, watermark, nsfw, nude, nipples"
SEED = 44                      # the one that came out coloured, hands together, mid-grin
SPRITE = os.path.join(REPO, "assets", "nami_money_sprite.png")
EYES = [(362, 327), (510, 335)]  # iris centres in the 832x1216 render (measured)
IRIS_R = 20
HANDS = (370, 455, 545, 665)     # the clasped hands, in the same render
FPS = 12
CYCLE = 2.0                       # seconds; every motion below is periodic in it
DURATION = 3.6                    # the effect length (the sound's)


def generate(dst):
    from sqlalchemy import text
    from app.database import SessionLocal
    db = SessionLocal()
    key = db.execute(text("select k.key from api_keys k join users u on u.id=k.user_id "
                          "where u.is_admin and k.is_active order by k.id limit 1")).scalar()
    db.close()
    body = json.dumps({"prompt": PROMPT, "negative_prompt": NEGATIVE, "width": 832, "height": 1216, "seed": SEED}).encode()
    req = urllib.request.Request("http://127.0.0.1:3051/api/generate-image", data=body,
                                 headers={"Content-Type": "application/json", "X-API-Key": key})
    with urllib.request.urlopen(req, timeout=1200) as r:
        d = json.load(r)
    if not d.get("image"):
        raise RuntimeError(f"image generation failed: {d.get('error')}")
    open(dst, "wb").write(base64.b64decode(d["image"].split(",")[-1]))


def _bag(r):
    """One money bag the size of an iris: gold, tied, a green $ and a glint so it still reads as an EYE."""
    from PIL import Image, ImageDraw, ImageFont
    ss = 4; S = int(r * 2 * ss); im = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    o, gold, dark, w, cx = (40, 25, 10, 255), (214, 170, 74, 255), (160, 118, 40, 255), max(2, int(ss * 1.6)), S / 2
    d.ellipse([S * .12, S * .34, S * .88, S * .98], fill=gold, outline=o, width=w)
    d.ellipse([S * .52, S * .55, S * .84, S * .95], fill=dark)
    d.polygon([(cx - S * .13, S * .36), (cx + S * .13, S * .36), (cx + S * .07, S * .22), (cx - S * .07, S * .22)], fill=gold, outline=o)
    d.polygon([(cx - S * .07, S * .22), (cx - S * .26, S * .06), (cx - S * .02, S * .15), (cx + S * .02, S * .15),
               (cx + S * .26, S * .06), (cx + S * .07, S * .22)], fill=gold, outline=o)
    d.rectangle([cx - S * .15, S * .33, cx + S * .15, S * .39], fill=(170, 40, 40, 255))
    try:
        f = ImageFont.truetype("/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf", int(S * .55))
    except Exception:
        f = ImageFont.load_default()
    d.text((cx, S * .70), "$", fill=(20, 110, 40, 255), font=f, anchor="mm", stroke_width=max(1, ss // 2),
           stroke_fill=(255, 240, 180, 255))
    d.ellipse([S * .22, S * .42, S * .36, S * .56], fill=(255, 255, 255, 235))
    return im.resize((int(r * 2), int(r * 2)), Image.LANCZOS)


def make_sprite(raw, dst):
    """Money-bag pupils, then the cut-out; the hand box is recorded in the sprite's own coordinates."""
    import io
    from PIL import Image
    from rembg import remove
    im = Image.open(raw).convert("RGBA")
    for x, y in EYES:
        b = _bag(IRIS_R * 1.5)
        im.alpha_composite(b, (int(x - b.width / 2), int(y - b.height / 2) - 2))
    buf = io.BytesIO(); im.save(buf, "PNG")
    cut = Image.open(io.BytesIO(remove(buf.getvalue()))).convert("RGBA")
    bb = cut.getbbox() or (0, 0, cut.width, cut.height)
    cut = cut.crop(bb)
    cut.save(dst)
    json.dump({"hands": [HANDS[0] - bb[0], HANDS[1] - bb[1], HANDS[2] - bb[0], HANDS[3] - bb[1]]},
              open(dst + ".json", "w"))


def render(sprite_path, out_dir, max_h=640):
    from PIL import Image, ImageDraw, ImageFilter
    sprite = Image.open(sprite_path).convert("RGBA")
    hx0, hy0, hx1, hy1 = json.load(open(sprite_path + ".json"))["hands"]
    k = min(1.0, max_h / sprite.height)
    if k < 1:
        sprite = sprite.resize((int(sprite.width * k), int(sprite.height * k)), Image.LANCZOS)
        hx0, hy0, hx1, hy1 = [int(v * k) for v in (hx0, hy0, hx1, hy1)]
    sw, sh = sprite.size
    hands = sprite.crop((hx0, hy0, hx1, hy1))
    mask = Image.new("L", hands.size, 0)
    ImageDraw.Draw(mask).ellipse([4, 4, hands.width - 4, hands.height - 4], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(6))
    W, H = int(sw * 1.3) // 2 * 2, int(sh * 1.12) // 2 * 2
    rng = np.random.default_rng(5)
    coins = [(rng.uniform(0.05, 0.95), rng.uniform(0, 1), rng.uniform(0.5, 1.0)) for _ in range(9)]
    n = int(round(CYCLE * FPS))
    for i in range(n):
        t = i / FPS
        frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        body = sprite.copy()
        dx = 0.022 * sw * math.sin(2 * math.pi * 3 * t / CYCLE * 2)      # the rub: 3 strokes a second
        dy = 0.006 * sh * abs(math.sin(2 * math.pi * 3 * t / CYCLE * 2))
        layer = Image.new("RGBA", body.size, (0, 0, 0, 0))
        layer.paste(hands, (int(hx0 + dx), int(hy0 - dy)), mask)
        body.alpha_composite(layer)
        bob = 0.012 * sh * math.sin(2 * math.pi * t / CYCLE * 2)
        frame.alpha_composite(body, ((W - sw) // 2, int(H - sh - 0.02 * sh + bob)))
        d = ImageDraw.Draw(frame)
        for fx, ph, sp in coins:                                     # coins drift up, one cycle each
            p = (ph + t / CYCLE * sp * 2) % 1.0
            x, y, r = fx * W, H * (1 - p), 9 + 5 * sp
            a = int(255 * min(1, 4 * p) * min(1, 4 * (1 - p)))
            d.ellipse([x - r, y - r, x + r, y + r], fill=(245, 200, 60, a), outline=(150, 100, 20, a), width=2)
            d.text((x, y), "$", fill=(120, 80, 10, a), anchor="mm")
        frame.save(os.path.join(out_dir, f"f{i:04d}.png"))
    return n


def kaching(path, voice=None):
    """A cash-register 'ka-ching' (a click, then a bright two-bell ring), voice optional on top."""
    sr = 44100; n = int(DURATION * sr); t = np.arange(n) / sr; out = np.zeros(n)
    click = np.random.default_rng(1).standard_normal(int(0.03 * sr)) * np.exp(-np.arange(int(0.03 * sr)) / (0.004 * sr))
    out[int(0.05 * sr):int(0.05 * sr) + len(click)] += click * 0.6
    for at, f in ((0.12, 2637.0), (0.16, 3520.0)):
        s = int(at * sr); tt = t[: n - s]
        out[s:] += 0.35 * np.sin(2 * np.pi * f * tt) * np.exp(-tt * 3.0) * (1 + 0.5 * np.sin(2 * np.pi * 7 * tt))
    if voice and os.path.exists(voice):
        with wave.open(voice) as w:
            v = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32768
            if w.getnchannels() > 1:
                v = v.reshape(-1, w.getnchannels()).mean(1)
        s = int(0.9 * sr); m = min(len(v), n - s)
        out[s:s + m] += v[:m] / (np.abs(v).max() + 1e-9) * 0.8
    out /= np.abs(out).max() / 0.9
    fade = int(0.3 * sr); out[-fade:] *= np.linspace(1, 0, fade)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((out * 32767).astype("<i2").tobytes())


def run(cmd):
    print("+", " ".join(cmd)); subprocess.run(cmd, check=True)


def main():
    tmp = tempfile.mkdtemp(prefix="nami_asset_")
    frames = os.path.join(tmp, "frames"); os.makedirs(frames)
    if "--redraw" in sys.argv or not os.path.exists(SPRITE):
        raw = os.path.join(tmp, "raw.png"); generate(raw); make_sprite(raw, SPRITE)
        print("drew a NEW Nami ->", SPRITE)
    n = render(SPRITE, frames)
    run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", os.path.join(frames, "f%04d.png"),
         "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-qscale:v", "16",
         os.path.join(REPO, "assets", "nami_money.mov")])
    voice = None
    try:
        mp3 = os.path.join(tmp, "v.mp3"); voice = os.path.join(tmp, "v.wav")
        run([os.path.join(REPO, "venv-unified", "bin", "edge-tts"), "--voice", "ja-JP-NanamiNeural",
             "--pitch=+30Hz", "--text", "うふふ〜♪ お金〜！", "--write-media", mp3])
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-ac", "1", "-ar", "44100", voice])
    except Exception as e:
        print("no voice line:", e); voice = None
    wav = os.path.join(tmp, "a.wav"); kaching(wav, voice)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-ac", "2", "-b:a", "160k",
         os.path.join(REPO, "assets", "nami_money.mp3")])
    print(f"wrote assets/nami_money.mov ({n} frames) + assets/nami_money.mp3")


if __name__ == "__main__":
    sys.exit(main())
