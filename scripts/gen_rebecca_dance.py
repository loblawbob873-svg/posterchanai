"""Regenerate assets/rebecca_dance.mov + assets/rebecca.mp3 — the `rebecca` effect.

The character is NOT drawn by hand (that is what made the old reze/theraped art look like doodles)
and there is no green-screen dance footage of her to key, so this takes the third route: the sprite
is rendered by THIS NODE'S OWN anime image model (`POST /api/generate-image`, prompt kept below),
cut out with the same rembg the `removebackground` command uses, then *animated* — a beat-locked
hop/sway/tilt/squash cycle, which is how sprite dances read as dancing without any new drawings.

The music is the Edgerunners theme, cut from a detected downbeat, and the hop period is that same
detected tempo (125 BPM → 0.48 s/beat) so she lands on the beat.

Output: alpha ProRes 4444 (yuva444p10le — webm alpha is silently dropped by this ffmpeg build),
12 fps (what image_gif_overlay_video resamples overlays to anyway). Unlike `vibe`, whose asset is
keyed footage and must therefore be as long as the whole effect, this motion is PERIODIC, so the
asset is exactly ONE 4-beat cycle (~1.9 s) that `-stream_loop -1` repeats seamlessly — a 23-frame
file instead of a 96-frame one, i.e. ~3 MB in git instead of ~14 MB.

Run from the repo root:  venv-unified/bin/python scripts/gen_rebecca_dance.py
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

# Naming the character and letting the model recall her beats describing her: a long "platinum
# blonde hair, cybernetic arms, cropped hoodie" prompt fought the model's own knowledge and produced
# a generic white-haired girl. This wording gets the mint twin-tails, the black-and-yellow bomber and
# the chunky boots right; everything after the first three sentences is only what the EFFECT needs.
PROMPT = ("Rebecca from Cyberpunk Edgerunners. Cute. Anime. Full body, standing, giving a big "
          "thumbs up, plain white background")
NEGATIVE = ("text, letters, watermark, signature, blurry, monochrome, greyscale, sketch, lineart, "
            "extra fingers, deformed hands, malformed limbs, cropped, out of frame, multiple people, "
            "photo, realistic, 3d render, busy background")

MUSIC_URL = "https://www.youtube.com/watch?v=gzbLODUb1sA"   # Rosa Walton — I Really Want to Stay at Your House
MUSIC_START = 63.15      # a detected downbeat inside the chorus
DURATION = 8.0
FPS = 12
BEAT = 60.0 / 125.0      # detected tempo of the chorus


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
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    if not d.get("image"):
        raise RuntimeError(f"image generation failed: {d.get('error')}")
    open(dst, "wb").write(base64.b64decode(d["image"]))


def cut_out(src, dst, max_h=900):
    """Background removal + trim to the silhouette (same rembg as `removebackground`)."""
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

    Everything is a continuous function of TIME (not of frame index), so the motion is smooth at any
    fps and the clip needs no loop point — the asset is rendered at exactly the effect's length.
    """
    from PIL import Image
    sprite = Image.open(sprite_path).convert("RGBA")
    sw, sh = sprite.size
    lift = 0.075 * sh                       # hop height
    sway = 0.055 * sw                       # side-to-side travel
    tilt = 7.0                              # degrees, leaning into the sway
    W = int(sw * 1.55) // 2 * 2
    H = int(sh * 1.16) // 2 * 2
    # ONE sway/tilt cycle (4 beats) — hop and sway are both back at their start by then, so the file
    # loops seamlessly and only ~23 frames ever have to ship. 4*BEAT*FPS is 23.04 frames, and rounding
    # to 23 makes the loop run 0.2% fast: 0.07 of a beat of drift across the whole 8 s clip.
    n = int(round(4 * BEAT * FPS))
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
    tmp = tempfile.mkdtemp(prefix="rebecca_asset_")
    frames = os.path.join(tmp, "frames")
    os.makedirs(frames)
    raw, sprite = os.path.join(tmp, "raw.png"), os.path.join(tmp, "sprite.png")

    generate_sprite(raw)
    # 620px, not the 900 default: the sprite's height drives the frame size, and 900 made the
    # ProRes asset ~14 MB for no visible gain (the overlay renders at ~0.7x the photo height).
    cut_out(raw, sprite, max_h=620)
    n = render_dance(sprite, frames)
    print(f"rendered {n} frames")

    run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", os.path.join(frames, "f%04d.png"),
         "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-qscale:v", "16",
         os.path.join(REPO, "assets", "rebecca_dance.mov")])

    music = os.path.join(tmp, "song.mp3")
    ytdlp = os.path.join(REPO, "venv-unified", "bin", "yt-dlp")
    run([ytdlp if os.path.exists(ytdlp) else "yt-dlp", "-f", "ba/b", "-x", "--audio-format", "mp3",
         "--audio-quality", "0", "-o", music.replace(".mp3", ".%(ext)s"), MUSIC_URL])
    run(["ffmpeg", "-y", "-ss", str(MUSIC_START), "-t", str(DURATION), "-i", music, "-vn",
         "-af", f"afade=t=out:st={DURATION - 0.25}:d=0.25,loudnorm=I=-16:TP=-1.5:LRA=11",
         "-ac", "2", "-ar", "44100", "-b:a", "192k", os.path.join(REPO, "assets", "rebecca.mp3")])

    print("wrote assets/rebecca_dance.mov + assets/rebecca.mp3")


if __name__ == "__main__":
    sys.exit(main())
