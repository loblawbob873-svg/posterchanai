"""Regenerate assets/uwu_dance.mov + assets/uwu.mp3 — the `uwu` effect.

Same third route as `rebecca` (see gen_rebecca_dance.py): there is no green-screen footage of an
original character to key and hand-drawn sprites read as doodles, so the girl is rendered by THIS
NODE'S OWN anime image model (`POST /api/generate-image`), cut out with the same rembg the
`removebackground` command uses, then *animated* — a hop/sway/tilt/squash cycle, which is what makes
a still sprite read as dancing without a second drawing.

She is an ORIGINAL character, deliberately not one of the ones we already ship (rebecca/reze/vibe/
makima): pink hoodie, cat-ear headband, twin tails, finger hearts.

**The sprite is committed (assets/uwu_sprite.png) and reused by default.** Image generation is not
deterministic, so re-running this without that file draws a DIFFERENT girl and silently changes who
the effect is. Pass --redraw only when you actually want a new character.

Audio is a 5s "uwu" voice clip; the effect is that long because the clip is (74% of it is voice —
the giggle clips that turn up in the same search are 11s of mostly silence at a peak of 0.05).
There is no musical beat to lock to, so the hop runs at a bouncy 130 BPM.

Output: alpha ProRes 4444 (yuva444p10le — webm alpha is silently dropped by this ffmpeg build),
12 fps (what image_gif_overlay_video resamples overlays to anyway). The motion is PERIODIC, so the
asset is exactly ONE 4-beat cycle that `-stream_loop -1` repeats seamlessly — 22 frames instead of 58.

Run from the repo root:  venv-unified/bin/python scripts/gen_uwu_dance.py [--redraw]
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

# Generic figure, so the prompt leads with "full body anime girl" and the STYLE words — naming a
# character only helps when the model already knows them. "anime" has to be in there (it selects the
# anime model) and lineart/monochrome have to be in the NEGATIVE, or asking for cel art returns
# uncoloured line drawings.
PROMPT = ("full body anime girl, adult, cute, pastel pink oversized hoodie, cat ear headband, "
          "long twin tails, big sparkling eyes, blushing, happy open smile, making a finger heart, "
          "standing, clean anime cel shading, anime key visual, plain white background")
NEGATIVE = ("text, letters, watermark, signature, blurry, monochrome, greyscale, sketch, lineart, "
            "extra fingers, deformed hands, malformed limbs, cropped, out of frame, multiple people, "
            "photo, realistic, 3d render, painterly, busy background, nsfw")

SPRITE = os.path.join(REPO, "assets", "uwu_sprite.png")

AUDIO_URL = "https://www.youtube.com/watch?v=zDbk8DHWH4U"   # "UWU (anime girls)" sound effect
AUDIO_START = 0.30       # skip the silent head
DURATION = 4.8           # the clip's usable length
FPS = 12
BEAT = 60.0 / 130.0      # no beat to detect in a voice clip — a bouncy tempo, chosen


def generate_sprite(dst):
    """Ask the local node for the character art (same endpoint the `geni` command uses)."""
    from sqlalchemy import text
    from app.database import SessionLocal
    db = SessionLocal()
    key = db.execute(text("select key from api_keys where user_id=1 order by id limit 1")).scalar()
    db.close()
    body = json.dumps({"prompt": PROMPT, "negative_prompt": NEGATIVE,
                       "width": 768, "height": 1152, "steps": 30}).encode()
    req = urllib.request.Request("http://127.0.0.1:3051/api/generate-image", data=body,
                                 headers={"Content-Type": "application/json", "X-API-Key": key})
    with urllib.request.urlopen(req, timeout=1200) as r:
        d = json.load(r)
    if not d.get("image"):
        raise RuntimeError(f"image generation failed: {d.get('error')}")
    open(dst, "wb").write(base64.b64decode(d["image"]))


def cut_out(src, dst, max_h=620):
    """Background removal + trim to the silhouette (same rembg as `removebackground`).

    620px, not the 900 the first cut used: the sprite's height drives the frame size, and the overlay
    renders at well under the photo's height anyway, so bigger only inflates the ProRes asset."""
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


def render_dance(sprite_path, out_dir):
    """One hop per beat, with the sway/tilt on a slower 4-beat cycle so it never looks like a metronome.

    Everything is a continuous function of TIME, so the cycle closes exactly and the file loops with
    no seam. Ported from gen_rebecca_dance.render_dance — same motion, her own proportions.
    """
    from PIL import Image
    sprite = Image.open(sprite_path).convert("RGBA")
    sw, sh = sprite.size
    lift = 0.075 * sh                       # hop height
    sway = 0.055 * sw                       # side-to-side travel
    tilt = 7.0                              # degrees, leaning into the sway
    W = int(sw * 1.55) // 2 * 2
    H = int(sh * 1.16) // 2 * 2
    n = int(round(4 * BEAT * FPS))          # ONE sway/tilt cycle; hop is back at its start too
    for i in range(n):
        t = i / FPS
        b = t / BEAT                                  # position in beats
        hop = abs(math.sin(math.pi * b)) ** 0.7       # one hop per beat, snappy arc
        slow = math.sin(math.pi * b / 2)              # 4-beat sway/tilt cycle
        sy = 1.0 - 0.06 * (1.0 - hop)                 # squash on landing, stretch at the apex
        sx = 1.0 / sy                                 # keep her volume constant
        frame_w, frame_h = max(2, int(sw * sx)), max(2, int(sh * sy))
        art = sprite.resize((frame_w, frame_h), Image.LANCZOS)
        art = art.rotate(-tilt * slow, resample=Image.BICUBIC, expand=True)
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        # anchor her FEET: bottom-centre of the canvas, raised by the hop. The overlay is
        # bottom-anchored on the photo, so any gap under the feet would read as her floating.
        x = int((W - art.width) / 2 + sway * slow)
        y = int(H - art.height - lift * hop)
        canvas.alpha_composite(art, (x, max(0, y)))
        canvas.save(os.path.join(out_dir, f"f{i:04d}.png"))
    return n


def run(cmd):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    redraw = "--redraw" in sys.argv
    tmp = tempfile.mkdtemp(prefix="uwu_asset_")
    frames = os.path.join(tmp, "frames")
    os.makedirs(frames)

    if redraw or not os.path.exists(SPRITE):
        raw = os.path.join(tmp, "raw.png")
        generate_sprite(raw)
        cut_out(raw, SPRITE)
        print(f"drew a NEW character -> {SPRITE}")
    else:
        print(f"reusing {SPRITE} (pass --redraw to draw a different girl)")

    n = render_dance(SPRITE, frames)
    print(f"rendered {n} frames")

    run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", os.path.join(frames, "f%04d.png"),
         "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-qscale:v", "16",
         os.path.join(REPO, "assets", "uwu_dance.mov")])

    clip = os.path.join(tmp, "uwu.mp3")
    ytdlp = os.path.join(REPO, "venv-unified", "bin", "yt-dlp")
    run([ytdlp if os.path.exists(ytdlp) else "yt-dlp", "-f", "ba/b", "-x", "--audio-format", "mp3",
         "--audio-quality", "0", "-o", clip.replace(".mp3", ".%(ext)s"), AUDIO_URL])
    run(["ffmpeg", "-y", "-ss", str(AUDIO_START), "-t", str(DURATION), "-i", clip, "-vn",
         "-af", f"afade=t=out:st={DURATION - 0.25}:d=0.25,loudnorm=I=-16:TP=-1.5:LRA=11",
         "-ac", "2", "-ar", "44100", "-b:a", "192k", os.path.join(REPO, "assets", "uwu.mp3")])

    print("wrote assets/uwu_dance.mov + assets/uwu.mp3")


if __name__ == "__main__":
    sys.exit(main())
