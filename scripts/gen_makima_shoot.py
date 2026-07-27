"""Regenerate assets/makima_shoot.mov + assets/makima.mp3 — the `makima` effect.

Makima finger-gunning the viewer. Same route as `rebecca` (scripts/gen_rebecca_dance.py): the
sprite comes from THIS NODE'S OWN anime image model and rembg, then the motion is added here —
there is no green-screen footage of this to key, and hand-drawing it is what made the old effects
look like doodles.

The motion is a RECOIL: on every shot she kicks back (a fast decaying jolt — scale, lift, twist)
and a muzzle flash pops at each fingertip. Fingertips are found from the alpha channel, not
hardcoded: in this pose her arms are the widest thing in the upper third, so the extreme opaque
pixels of that band ARE her fingertips.

The audio is bare gunshots (no music — see build_audio), placed at exactly the frames the visual
fires on, so the bangs and the flashes cannot drift apart however the loop lands.

Output: alpha ProRes 4444 (yuva444p10le — webm alpha is silently dropped by this ffmpeg build),
12 fps, exactly ONE 4-beat cycle that `-stream_loop -1` repeats (21 frames, ~3 MB, versus ~14 MB
for a full 8 s render).

Run from the repo root:  venv-unified/bin/python scripts/gen_makima_shoot.py
"""
import base64
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Naming the character beats describing her — see gen_rebecca_dance.py for what a long
# describe-her-features prompt does to the likeness.
PROMPT = ("Makima from Chainsaw Man. Cute. Anime. Full body, aiming a finger gun at you, "
          "one eye closed, playful smirk, plain white background")
NEGATIVE = ("text, letters, watermark, signature, blurry, monochrome, greyscale, sketch, lineart, "
            "extra fingers, deformed hands, malformed limbs, cropped, out of frame, multiple people, "
            "photo, realistic, 3d render, busy background")

SHOT_URL = "https://www.youtube.com/watch?v=RQvwcqEvn9g"    # single pistol shot SFX
DURATION = 8.0
FPS = 12
# No music (see build_audio), so the "beat" is just the firing rhythm — this is the tempo the
# Chainsaw Man OP was detected at, kept because it paces the shots nicely.
BEAT = 60.0 / 136.0
CYCLE_FRAMES = int(round(4 * BEAT * FPS))            # 21 — one bar, the loop length
SHOT_FRAMES = [0, int(round(2 * BEAT * FPS))]        # she fires on beats 1 and 3 of the bar


def generate_sprite(dst):
    from sqlalchemy import text
    from app.database import SessionLocal
    db = SessionLocal()
    key = db.execute(text("select key from api_keys where user_id=1 order by id limit 1")).scalar()
    db.close()
    body = json.dumps({"prompt": PROMPT, "negative_prompt": NEGATIVE,
                       "width": 768, "height": 1152, "steps": 30}).encode()
    req = urllib.request.Request("http://127.0.0.1:3051/api/generate-image", data=body,
                                 headers={"Content-Type": "application/json", "X-API-Key": key})
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    if not d.get("image"):
        raise RuntimeError(f"image generation failed: {d.get('error')}")
    open(dst, "wb").write(base64.b64decode(d["image"]))


def cut_out(src, dst, max_h=620):
    import io
    from PIL import Image
    from rembg import remove
    im = Image.open(io.BytesIO(remove(open(src, "rb").read()))).convert("RGBA")
    bb = im.getbbox()
    if bb:
        im = im.crop(bb)
    if im.height > max_h:
        im = im.resize((max(1, int(im.width * max_h / im.height)), max_h), Image.LANCZOS)
    im.save(dst)


def find_fingertips(sprite):
    """The two hands, as (x, y) in sprite pixels — the extreme opaque pixels of the arm band."""
    import numpy as np
    a = np.asarray(sprite)[:, :, 3]
    h, w = a.shape
    top, bot = int(h * 0.12), int(h * 0.45)
    band = a[top:bot]
    ys, xs = np.where(band > 128)
    if len(xs) < 10:
        return [(int(w * 0.1), int(h * 0.2)), (int(w * 0.9), int(h * 0.2))]
    out = []
    for tx in (xs.min(), xs.max()):
        out.append((int(tx), int(ys[xs == tx].mean()) + top))
    return out


def draw_flash(canvas, cx, cy, size, outward, strength):
    """A comic muzzle flash: a four-point star plus a hot core, thrown slightly outward from the
    hand so it reads as coming OUT of the finger rather than sitting on it."""
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx += int(outward * size * 0.35)
    a = int(255 * strength)
    for r, col in ((size, (255, 214, 92, int(a * 0.85))), (size * 0.55, (255, 255, 235, a))):
        pts = []
        for i in range(8):
            ang = math.pi * i / 4
            rad = r if i % 2 == 0 else r * 0.34
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        d.polygon(pts, fill=col)
    d.ellipse([cx - size * 0.22, cy - size * 0.22, cx + size * 0.22, cy + size * 0.22],
              fill=(255, 255, 255, a))
    canvas.alpha_composite(layer)


def render_shots(sprite_path, out_dir):
    """One bar of frames: idle sway, a recoil jolt on each shot frame, flashes on the hands."""
    from PIL import Image
    sprite = Image.open(sprite_path).convert("RGBA")
    sw, sh = sprite.size
    tips = find_fingertips(sprite)
    W = int(sw * 1.30) // 2 * 2
    H = int(sh * 1.14) // 2 * 2
    flash = sh * 0.075
    for i in range(CYCLE_FRAMES):
        # time since the most recent shot, wrapping around the loop
        since = min((i - s) % CYCLE_FRAMES for s in SHOT_FRAMES) / FPS
        kick = math.exp(-since / 0.085)               # sharp attack, ~2 frames of decay
        idle = math.sin(2 * math.pi * i / CYCLE_FRAMES)
        scale = 1.0 + 0.045 * kick
        art = sprite.resize((max(2, int(sw * scale)), max(2, int(sh * scale))), Image.LANCZOS)
        art = art.rotate(-1.8 * kick + 0.8 * idle, resample=Image.BICUBIC, expand=True)
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        x = int((W - art.width) / 2 + 0.012 * sw * idle)
        y = int(H - art.height - 0.030 * sh * kick)   # the recoil lifts her off her stance
        canvas.alpha_composite(art, (x, max(0, y)))
        # The flash gets its OWN, slower decay than the recoil: at one or two frames it was a
        # single-frame event, and a single frame is exactly what an fps resample can drop — half
        # the shots came out with no flash at all. Three frames (~0.25s) still reads as a pop.
        glow = math.exp(-since / 0.16)
        if glow > 0.30:
            for (tx, ty), outward in zip(tips, (-1, 1)):
                fx = x + int(tx * art.width / sw)
                fy = max(0, y) + int(ty * art.height / sh)
                draw_flash(canvas, fx, fy, flash, outward, min(1.0, glow))
        canvas.save(os.path.join(out_dir, f"f{i:04d}.png"))
    return CYCLE_FRAMES


def shot_times():
    """Every moment the LOOPED overlay fires, across the whole clip — the gunshots go here."""
    out, k = [], 0
    while True:
        base = k * CYCLE_FRAMES / FPS
        if base >= DURATION:
            return out
        for s in SHOT_FRAMES:
            t = base + s / FPS
            if t < DURATION - 0.15:
                out.append(t)
        k += 1


def run(cmd):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def build_audio(tmp, dst):
    """Gunshots on silence — no music bed.

    A track was tried first and cut: KICK BACK is dense and loud, so even at +14 dB the bangs only
    sat ~5 dB over it and read as part of the drums. Bare shots also make the effect land anywhere
    it's posted (no music under someone else's video) and keep the joke on the gag, not the song.
    """
    ytdlp = os.path.join(REPO, "venv-unified", "bin", "yt-dlp")
    ytdlp = ytdlp if os.path.exists(ytdlp) else "yt-dlp"
    shot_raw = os.path.join(tmp, "shot_raw.mp3")
    run([ytdlp, "-f", "ba/b", "-x", "--audio-format", "mp3",
         "-o", shot_raw.replace(".mp3", ".%(ext)s"), SHOT_URL])
    # The SFX upload has silence around the bang: keep the loud part only.
    shot = os.path.join(tmp, "shot.wav")
    run(["ffmpeg", "-y", "-i", shot_raw, "-af",
         "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02,atrim=0:0.45",
         "-ac", "2", "-ar", "44100", shot])

    times = shot_times()
    n = len(times)
    # The sample already peaks at 0 dBFS and the shots never overlap, so no normalisation pass —
    # loudnorm on a mostly-silent track would just chase the -16 LUFS target and pump the noise
    # floor between bangs. A little headroom (0.9) is all that's needed.
    parts = [f"[1:a]volume=0.9,asplit={n}" + "".join(f"[s{i}]" for i in range(n))]
    for i, t in enumerate(times):
        ms = int(round(t * 1000))
        parts.append(f"[s{i}]adelay={ms}|{ms}[d{i}]")
    mix_in = "[0:a]" + "".join(f"[d{i}]" for i in range(n))
    parts.append(f"{mix_in}amix=inputs={n + 1}:normalize=0:duration=first[mixed]")
    parts.append(f"[mixed]atrim=0:{DURATION}[out]")
    run(["ffmpeg", "-y", "-f", "lavfi", "-t", str(DURATION), "-i", "anullsrc=r=44100:cl=stereo",
         "-i", shot, "-filter_complex", ";".join(parts), "-map", "[out]",
         "-ac", "2", "-ar", "44100", "-b:a", "192k", dst])


def main():
    tmp = tempfile.mkdtemp(prefix="makima_asset_")
    frames = os.path.join(tmp, "frames")
    os.makedirs(frames)
    raw, sprite = os.path.join(tmp, "raw.png"), os.path.join(tmp, "sprite.png")

    generate_sprite(raw)
    cut_out(raw, sprite)
    n = render_shots(sprite, frames)
    print(f"rendered {n} frames, shots at {[round(t, 3) for t in shot_times()]}")

    run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", os.path.join(frames, "f%04d.png"),
         "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-qscale:v", "16",
         os.path.join(REPO, "assets", "makima_shoot.mov")])
    build_audio(tmp, os.path.join(REPO, "assets", "makima.mp3"))
    print("wrote assets/makima_shoot.mov + assets/makima.mp3")


if __name__ == "__main__":
    sys.exit(main())
