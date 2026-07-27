"""Regenerate assets/vibe_dance.mov + assets/vibe.mp3 — the `vibe` effect's cute anime dancer.

Unlike gen_reze_dance.py (which DRAWS chibi figures with Pillow, which is why reze looks like
doodles), this one keys REAL cel-animation off a green screen, so the overlay actually looks
like anime. Two source clips:

  * the dance  — a 1080p60 green-screen upload of the Kaguya-sama "Chika dance" close-up
  * the music  — the same show's ED, from 14.09s (where the band kicks in) for exactly 8s

Output: 504x560 ProRes 4444 (yuva444p10le, real alpha — NOT webm, whose alpha this ffmpeg build
silently drops), 12 fps (what image_gif_overlay_video resamples overlays to anyway) and stretched
to 8.06s so an 8.0s render plays it exactly once with no loop seam.

Run from the repo root with the venv that has yt-dlp:  venv-unified/bin/python scripts/gen_vibe_dance.py
"""
import os
import subprocess
import sys
import tempfile

DANCE_URL = "https://www.youtube.com/watch?v=v4YYrwrkbr8"   # "Chika Dancing Green Screen"
MUSIC_URL = "https://www.youtube.com/watch?v=eqjFmsZGBSc"   # creditless ED (Chikatto Chika Chika)
MUSIC_START, MUSIC_LEN = 14.09, 8.0

# The green screen sampled off the source: RGB(31,237,20). The crop is the union bounding box of
# the character across every frame of the 1920x1080 source (she is cut off at the bottom edge,
# which is fine — the overlay is bottom-anchored).
CROP = "crop=954:1060:942:20"
KEY = "format=rgba,chromakey=color=0x1FED14:similarity=0.14:blend=0.04,despill=type=green:mix=0.6:expand=0.2"
SRC_DURATION = 7.4536  # the green-screen clip's own length; stretched to 8.06s below

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YTDLP = os.path.join(REPO, "venv-unified", "bin", "yt-dlp")


def run(cmd):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    ytdlp = YTDLP if os.path.exists(YTDLP) else "yt-dlp"
    tmp = tempfile.mkdtemp(prefix="vibe_asset_")
    dance = os.path.join(tmp, "dance.mp4")
    music = os.path.join(tmp, "ed.mp4")

    run([ytdlp, "-f", "299+140/bv*[height<=1080]+ba/b", "--merge-output-format", "mp4",
         "-o", dance, DANCE_URL])
    run([ytdlp, "-f", "b[height<=480]/bv[height<=480]+ba", "-o", music, MUSIC_URL])

    run(["ffmpeg", "-y", "-i", dance,
         "-vf", f"{CROP},{KEY},scale=504:560:flags=lanczos,fps=12,setpts=(8.06/{SRC_DURATION})*PTS",
         "-an", "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
         "-qscale:v", "16", os.path.join(REPO, "assets", "vibe_dance.mov")])

    run(["ffmpeg", "-y", "-ss", str(MUSIC_START), "-t", str(MUSIC_LEN), "-i", music, "-vn",
         "-af", "afade=t=out:st=7.75:d=0.25,loudnorm=I=-16:TP=-1.5:LRA=11",
         "-ac", "2", "-ar", "44100", "-b:a", "192k", os.path.join(REPO, "assets", "vibe.mp3")])

    print("wrote assets/vibe_dance.mov + assets/vibe.mp3")


if __name__ == "__main__":
    sys.exit(main())
