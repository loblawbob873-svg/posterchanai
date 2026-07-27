"""Regenerate assets/reze_dance.mov — the `reze` effect's dancing overlay.

This used to DRAW two chibi figures with Pillow, which is exactly why the effect looked like
doodles. It now keys the real Chainsaw Man movie ED dance off a green screen, the same way
scripts/gen_vibe_dance.py does, so the overlay is actual cel animation.

`assets/reze.mp3` is deliberately NOT regenerated here: it is a different recording from this
clip's audio track (envelope cross-correlation ~0.16), i.e. the effect's music is its own joke and
replacing it would change the effect rather than improve it.

Output: 370x520 ProRes 4444 (yuva444p10le — this ffmpeg build silently drops webm alpha), 12 fps
(what image_gif_overlay_video resamples overlays to anyway), 13.17s so it covers the effect's 13.0s
without `-stream_loop -1` ever restarting it mid-render.

Run from the repo root with the venv that has yt-dlp:  venv-unified/bin/python scripts/gen_reze_dance.py
"""
import os
import subprocess
import sys
import tempfile

DANCE_URL = "https://www.youtube.com/watch?v=CtX8hAu7cuA"   # "The Reze Dance (Greenscreen)", 1080p60

# Sampled off the source: the key is RGB(0,252,24). The crop is the union bounding box of the
# LARGEST keyed blob (so a watermark can't widen it) across the whole window below.
START, LENGTH = 2.0, 13.2
CROP = "crop=736:1036:600:40"
KEY = ("format=rgba,chromakey=color=0x00FC18:similarity=0.15:blend=0.04,"
       "despill=type=green:mix=0.6:expand=0.2")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YTDLP = os.path.join(REPO, "venv-unified", "bin", "yt-dlp")


def run(cmd):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    tmp = tempfile.mkdtemp(prefix="reze_asset_")
    src = os.path.join(tmp, "dance.mp4")
    run([YTDLP if os.path.exists(YTDLP) else "yt-dlp",
         "-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4", "-o", src, DANCE_URL])
    run(["ffmpeg", "-y", "-ss", str(START), "-t", str(LENGTH), "-i", src,
         "-vf", f"{CROP},{KEY},scale=370:520:flags=lanczos,fps=12",
         "-an", "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
         "-qscale:v", "16", os.path.join(REPO, "assets", "reze_dance.mov")])
    print("wrote assets/reze_dance.mov")


if __name__ == "__main__":
    sys.exit(main())
