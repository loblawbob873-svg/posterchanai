"""Regenerate assets/characters/theraped.png, would.png and shrug.png — the pointing-meme cast.

Both were hand-drawn with Pillow (flat shapes, stick legs), which is why the format read as a
doodle rather than a reaction image. There is no green-screen footage of "a character pointing
straight up" to key, so these come from THIS NODE'S OWN anime image model
(`POST /api/generate-image`) and are cut out with the same rembg the `removebackground` command
uses. `_composite_char_bottom_center` crops to the silhouette and sizes by height, so the canvas
just needs a clean alpha — no particular aspect ratio.

Diffusion is a lottery: this generates several candidates per character and writes them all to a
temp dir, then installs the one you pick (or the first, with --auto). LOOK at them — the hand is
where these models fail, and the whole format depends on the finger reading as "pointing at the
image above".

Run from the repo root:  venv-unified/bin/python scripts/gen_pointing_chars.py [--auto] [N]
"""
import base64
import io
import json
import os
import sys
import tempfile
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# "anime" has to be in the prompt for the good anime model to be picked up, and asking for
# "lineart" (a natural thing to write for cel art) makes it return UNCOLOURED line drawings.
NEGATIVE = ("text, letters, watermark, signature, blurry, monochrome, greyscale, sketch, lineart, "
            "painterly, oil painting, semi realistic, "
            "extra fingers, deformed hands, malformed limbs, cropped, out of frame, multiple people, "
            "photo, realistic, 3d render, busy background")
# Leading with "anime <noun>" and naming the cel shading matters as much as the character name:
# "Old rabbi ... . Anime." (name-last, the wording that nails Rebecca/Makima) came back as
# semi-realistic painted illustration, because "rabbi in a long coat" pulls that way hard.
CHARACTERS = {
    "theraped": ("anime girl standing, full body, cute schoolgirl in a sailor uniform, one arm raised "
                 "straight up pointing at the sky with her index finger, big cheerful smile, looking up, "
                 "vibrant colours, clean anime cel shading, anime key visual, simple flat white background"),
    "would": ("full body anime gentleman in a dark business suit and red tie, round glasses, grey hair, "
              "dignified older professor, standing straight, one arm raised, index finger pointing "
              "straight up, smug knowing smile, vibrant colours, clean anime cel shading, anime key "
              "visual, simple flat white background"),
    # `shrug` is the same renderer (_add_pointing_meme), just a palms-up pose instead of a point.
    "shrug": ("full body anime rabbi, old man with a long white beard, black wide-brim hat and long "
              "black coat, shrugging with both palms turned up at his sides, resigned wry smile, "
              "raised eyebrows, vibrant colours, clean anime cel shading, anime key visual, simple "
              "flat white background"),
}


def api_key():
    from sqlalchemy import text
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        return db.execute(text("select key from api_keys where user_id=1 order by id limit 1")).scalar()
    finally:
        db.close()


def generate(prompt, dst, key):
    body = json.dumps({"prompt": prompt, "negative_prompt": NEGATIVE,
                       "width": 768, "height": 1152, "steps": 30}).encode()
    req = urllib.request.Request("http://127.0.0.1:3051/api/generate-image", data=body,
                                 headers={"Content-Type": "application/json", "X-API-Key": key})
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    if not d.get("image"):
        raise RuntimeError(f"image generation failed: {d.get('error')}")
    open(dst, "wb").write(base64.b64decode(d["image"]))


def cut_out(src, dst, max_h=900):
    from PIL import Image
    from rembg import remove
    im = Image.open(io.BytesIO(remove(open(src, "rb").read()))).convert("RGBA")
    bb = im.getbbox()
    if bb:
        im = im.crop(bb)
    if im.height > max_h:
        im = im.resize((max(1, int(im.width * max_h / im.height)), max_h), Image.LANCZOS)
    im.save(dst)


def main():
    args = [a for a in sys.argv[1:]]
    auto = "--auto" in args
    n = next((int(a) for a in args if a.isdigit()), 4)
    tmp = tempfile.mkdtemp(prefix="pointing_chars_")
    key = api_key()
    for name, prompt in CHARACTERS.items():
        cuts = []
        for i in range(n):
            raw = os.path.join(tmp, f"{name}_{i}.png")
            cut = os.path.join(tmp, f"{name}_{i}_cut.png")
            generate(prompt, raw, key)
            cut_out(raw, cut)
            cuts.append(cut)
            print("  candidate", cut)
        if auto:
            dst = os.path.join(REPO, "assets", "characters", f"{name}.png")
            os.replace(cuts[0], dst)
            print("installed", dst)
    if not auto:
        print(f"\ncandidates in {tmp} — copy the best over assets/characters/<name>.png")


if __name__ == "__main__":
    sys.exit(main())
