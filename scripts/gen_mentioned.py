"""Regenerate assets/mentioned_cheer.mov + assets/mentioned_cheer.mp3 -- the `mentioned` effect.

The "<THING> MENTIONED" meme: an anime girl with her eyes shut, mouth open and both arms up, cheering because
something she loves came up ("MICHIGAN MENTIONED"). The caption is the person's word and is burned on at
request time (effects_service.add_mentioned); this script only makes the girl and the sound.

Same route as `nami` (scripts/gen_nami_money.py): drawn by THIS NODE'S OWN anime model (`POST
/api/generate-image`, the word "anime" selects it), cut out with rembg, then ANIMATED -- she hops twice a
second with a little squash on landing while confetti falls past her. Every motion is periodic in one 1 s
cycle, so the asset is ONE cycle that `-stream_loop -1` repeats with no seam.

**The sprite is committed (assets/mentioned_cheer_sprite.png) and reused by default.** Image generation is
not deterministic; re-running without it draws a different girl. --redraw only when that is the point.

The audio is synthesized here -- a party-horn toot and a crowd "yay" (shaped noise) -- with an optional voice
line (edge-tts ja-JP-NanamiNeural, 「やったー！」) on top. Output: alpha ProRes 4444, 12 fps.

Run from the repo root:  venv-unified/bin/python scripts/gen_mentioned.py [--redraw]
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

PROMPT = ("anime, masterpiece, best quality, cute anime girl, short brown hair with a yellow hair clip, "
          "eyes closed happily, mouth wide open cheering, both arms raised above her head in celebration, both fists fully visible with empty space above them, "
          "grey hoodie, upper body, cel-shaded, clean lineart, plain white background")
NEGATIVE = "lowres, bad hands, extra fingers, missing fingers, extra arms, deformed, blurry, text, watermark, nsfw"
SEED = int(os.environ.get("MENTIONED_SEED", "21"))  # both fists in frame, eyes shut, mouth open
SPRITE = os.path.join(REPO, "assets", "mentioned_cheer_sprite.png")
FPS = 12
CYCLE = 1.0                       # seconds; two hops
DURATION = 3.0                    # the effect length (the sound's)
CONFETTI = [(255, 80, 120), (80, 200, 255), (255, 220, 60), (120, 230, 120), (190, 120, 255)]


def generate(dst):
    from app.database import SessionLocal
    db = SessionLocal()
    from app.services import api_key_store
    key = api_key_store.pick_key(db, admin=True, active=True)
    db.close()
    body = json.dumps({"prompt": PROMPT, "negative_prompt": NEGATIVE, "width": 832, "height": 1216, "seed": SEED}).encode()
    req = urllib.request.Request("http://127.0.0.1:3051/api/generate-image", data=body,
                                 headers={"Content-Type": "application/json", "X-API-Key": key})
    with urllib.request.urlopen(req, timeout=1200) as r:
        d = json.load(r)
    if not d.get("image"):
        raise RuntimeError(f"image generation failed: {d.get('error')}")
    open(dst, "wb").write(base64.b64decode(d["image"].split(",")[-1]))


def make_sprite(raw, dst):
    import io
    from PIL import Image
    from rembg import remove
    buf = io.BytesIO(); Image.open(raw).convert("RGBA").save(buf, "PNG")
    cut = Image.open(io.BytesIO(remove(buf.getvalue()))).convert("RGBA")
    cut = cut.crop(cut.getbbox() or (0, 0, cut.width, cut.height))
    cut.save(dst)


def render(sprite_path, out_dir, max_h=640):
    from PIL import Image, ImageDraw
    sprite = Image.open(sprite_path).convert("RGBA")
    k = min(1.0, max_h / sprite.height)
    if k < 1:
        sprite = sprite.resize((int(sprite.width * k), int(sprite.height * k)), Image.LANCZOS)
    sw, sh = sprite.size
    W, H = int(sw * 1.25) // 2 * 2, int(sh * 1.09) // 2 * 2
    rng = np.random.default_rng(11)
    bits = [(rng.uniform(0, 1), rng.uniform(0, 1), rng.uniform(0.6, 1.0), rng.integers(len(CONFETTI)),
             rng.uniform(0, 2 * math.pi)) for _ in range(26)]
    n = int(round(CYCLE * FPS))
    for i in range(n):
        t = i / FPS
        frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ph = (2 * t / CYCLE) % 1.0                         # two hops a cycle
        hop = math.sin(math.pi * ph)                       # 0 on the ground, 1 at the top
        squash = 1 - 0.05 * max(0.0, 1 - ph * 6) - 0.05 * max(0.0, (ph - 0.85) / 0.15)
        body = sprite.resize((int(sw * (2 - squash)), int(sh * squash)), Image.LANCZOS)
        frame.alpha_composite(body, ((W - body.width) // 2, int(H - body.height - 0.08 * sh * hop)))
        d = ImageDraw.Draw(frame)
        for fx, fp, sp, ci, rot in bits:                  # confetti falls, one cycle each
            p = (fp + t / CYCLE * sp) % 1.0
            x = fx * W + 10 * math.sin(2 * math.pi * (p * 2 + fx))
            y = H * p
            a = int(255 * min(1, 5 * (1 - p)))
            w2, h2 = 7, 3 + 3 * abs(math.sin(rot + 2 * math.pi * p * 3))
            d.rectangle([x - w2, y - h2, x + w2, y + h2], fill=CONFETTI[ci] + (a,))
        frame.save(os.path.join(out_dir, f"f{i:04d}.png"))
    return n


def cheer(path, voice=None):
    """A party-horn toot and a crowd 'yay' (band-limited noise swelling then fading), voice optional on top."""
    sr = 44100; n = int(DURATION * sr); t = np.arange(n) / sr; out = np.zeros(n)
    hl = int(0.45 * sr); th = np.arange(hl) / sr                      # the horn: a buzzy rising toot
    f = 520 + 180 * th / th[-1]
    horn = np.sign(np.sin(2 * np.pi * np.cumsum(f) / sr)) * 0.25 * np.minimum(1, th * 40) * np.exp(-th * 2)
    out[int(0.02 * sr):int(0.02 * sr) + hl] += horn
    noise = np.random.default_rng(3).standard_normal(n)                # the crowd
    k = np.ones(40) / 40; crowd = np.convolve(noise, k, mode="same") - np.convolve(noise, np.ones(400) / 400, mode="same")
    env = np.clip((t - 0.1) / 0.4, 0, 1) * np.clip((DURATION - t) / 1.2, 0, 1)
    out += crowd * env * 0.9 * (1 + 0.3 * np.sin(2 * np.pi * 5 * t))
    if voice and os.path.exists(voice):
        with wave.open(voice) as w:
            v = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32768
            if w.getnchannels() > 1:
                v = v.reshape(-1, w.getnchannels()).mean(1)
        s = int(0.35 * sr); m = min(len(v), n - s)
        out[s:s + m] += v[:m] / (np.abs(v).max() + 1e-9) * 0.9
    out /= np.abs(out).max() / 0.9
    fade = int(0.3 * sr); out[-fade:] *= np.linspace(1, 0, fade)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((out * 32767).astype("<i2").tobytes())


def run(cmd):
    print("+", " ".join(cmd)); subprocess.run(cmd, check=True)


def main():
    tmp = tempfile.mkdtemp(prefix="mentioned_asset_")
    frames = os.path.join(tmp, "frames"); os.makedirs(frames)
    if "--redraw" in sys.argv or not os.path.exists(SPRITE):
        raw = os.path.join(tmp, "raw.png"); generate(raw)
        if "--raw-only" in sys.argv:
            print("raw drawing ->", raw); return 0
        make_sprite(raw, SPRITE)
        print("drew a NEW cheering girl ->", SPRITE)
    n = render(SPRITE, frames)
    run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", os.path.join(frames, "f%04d.png"),
         "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-qscale:v", "16",
         os.path.join(REPO, "assets", "mentioned_cheer.mov")])
    voice = None
    try:
        mp3 = os.path.join(tmp, "v.mp3"); voice = os.path.join(tmp, "v.wav")
        run([os.path.join(REPO, "venv-unified", "bin", "edge-tts"), "--voice", "ja-JP-NanamiNeural",
             "--pitch=+30Hz", "--text", "やったー！", "--write-media", mp3])
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-ac", "1", "-ar", "44100", voice])
    except Exception as e:
        print("no voice line:", e); voice = None
    wav = os.path.join(tmp, "a.wav"); cheer(wav, voice)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-ac", "2", "-b:a", "160k",
         os.path.join(REPO, "assets", "mentioned_cheer.mp3")])
    print(f"wrote assets/mentioned_cheer.mov ({n} frames) + assets/mentioned_cheer.mp3")


if __name__ == "__main__":
    sys.exit(main())
