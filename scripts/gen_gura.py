#!/usr/bin/env python
"""Build assets/gura_pog.mov + assets/gura.mp3 — the `gura` effect (Shark Pog).

Run ONCE locally; production only ever loads the two assets. Same provenance rule as
scripts/gen_ruckus.py and scripts/gen_makima_shoot.py.

Two sources, both named by the meme rather than invented here:
  * the cutout is Know Your Meme's "Shark Pog" photo, which already ships a real alpha channel, so
    there is nothing to key — https://knowyourmeme.com/photos/1925071-gawr-gura
  * the audio is the Gura "a" SFX (YouTube xSxKP5tEVHY).

Why the sprite is ANIMATED here rather than keyed from the Shark Pog video: that video is Gura on a
white fish-bone-patterned background, and her hair is white too — there is no colour that separates
her from it, so a key takes her hair with the background. The KYM cutout is already perfect, and the
documented route for a still sprite (see project_audio_clip_effects) is to animate it: she crouches
small, then POPS on the vocal onset and settles with a slow bob.

The audio's leading dead air is CLIPPED, and clipped by detection rather than by a hardcoded
timestamp — the rip starts with ~0.6 s of nothing before the "a", which would otherwise play as a
silent MP4 for a fifth of its length. Onset is the first 10 ms window above -40 dBFS, backed off by
one window so the attack transient survives (cutting exactly on the onset lops off the consonant and
it reads as a click). An 8 ms fade-in covers the splice.

The mp3 is then padded with silence PAST DURATION so it plays ONCE. image_gif_overlay_video loops
audio with `-stream_loop -1`, so a 1 s file in a 2.2 s clip would say "a a a", and a file even a
frame short of the clip says it twice. Both the trim and the padding are asserted at build time by
decoding the finished mp3 — see build_audio for the encoder bug that made that necessary.

Output: alpha ProRes 4444 (yuva444p10le — webm alpha is silently dropped by this ffmpeg build),
12 fps, the full DURATION rather than one loop cycle, because the pop is a one-shot rather than a
repeating beat.

Run from the repo root:  venv-unified/bin/python scripts/gen_gura.py
"""
import math
import os
import subprocess
import sys
import tempfile
import urllib.request
import wave

import numpy as np
from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_MOV = os.path.join(REPO, "assets", "gura_pog.mov")
OUT_MP3 = os.path.join(REPO, "assets", "gura.mp3")

POG_URL = "https://i.kym-cdn.com/photos/images/original/001/925/071/e07.png"
AUDIO_URL = "https://www.youtube.com/watch?v=xSxKP5tEVHY"      # "Gawr Gura 'A' sound effect"

DURATION = 2.2            # lead-in + the "a" and its reverb tail (~1.2 s) + a beat to settle
FPS = 12
LEAD = 0.30            # silence before the "a", so the pop has an anticipation beat
CANVAS = (560, 640)    # sprite fills 0.8 of this; the rest is headroom for the pop overshoot
FILL = 0.80


def fetch_pog(dst):
    req = urllib.request.Request(POG_URL, headers={"User-Agent": "Mozilla/5.0",
                                                   "Referer": "https://knowyourmeme.com/"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
        f.write(r.read())
    im = Image.open(dst).convert("RGBA")
    a = np.asarray(im)[:, :, 3]
    if (a > 200).mean() > 0.97:
        raise SystemExit("Shark Pog source has no usable alpha")
    print(f"  pog cutout {im.size}, opaque {(a > 200).mean()*100:.0f}%")
    return im.crop(im.getbbox())


def onset_seconds(wav_path: str) -> float:
    """Seconds of leading dead air before the "a" — see the module docstring."""
    w = wave.open(wav_path)
    sr, ch = w.getframerate(), w.getnchannels()
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    hop = max(1, int(sr * 0.01))
    for i in range(0, len(x) - hop, hop):
        seg = x[i:i + hop]
        if 20 * math.log10(math.sqrt((seg ** 2).mean()) + 1e-12) > -40:
            t = max(0.0, (i - hop) / sr)
            print(f"  vocal onset at {i/sr:.3f}s, cutting at {t:.3f}s "
                  f"({len(x)/sr:.2f}s source)")
            return t
    return 0.0


def build_audio(tmp: str):
    raw = os.path.join(tmp, "a.wav")
    subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "-x", "--audio-format", "wav",
                    "-o", os.path.join(tmp, "a.%(ext)s"), AUDIO_URL], check=True,
                   capture_output=True)
    start = onset_seconds(raw)
    # Filter to WAV, THEN encode to mp3 — two passes, not one. Filtering straight into libmp3lame
    # silently ate ~0.2 s off the FRONT of the clip: the lead-in came back as 0.05 s instead of 0.30
    # and the file measured 2.80 s instead of 3.00. Same filter chain into a .wav is exact. The
    # symptom is only visible by decoding the result, which is why it survived a "it rendered fine".
    #
    # `apad` runs past DURATION on purpose. image_gif_overlay_video loops the audio with
    # `-stream_loop -1`, so an mp3 even a frame SHORT of the clip wraps and says "a" a second time
    # near the end — mp3 frames are 26 ms and the encoder adds its own padding, so landing exactly on
    # DURATION is not something to rely on. Overshooting is free: the extra is silence and the clip
    # is cut to length anyway.
    wav = os.path.join(tmp, "trimmed.wav")
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", raw,
        "-af", (f"afade=t=in:st=0:d=0.008,loudnorm=I=-16:TP=-1.5:LRA=11,"
                f"adelay={int(LEAD*1000)}|{int(LEAD*1000)},apad"),
        "-t", f"{DURATION + 0.25:.3f}", "-ac", "2", "-ar", "44100", wav,
    ], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", wav,
                    "-ac", "2", "-ar", "44100", "-b:a", "192k", OUT_MP3], check=True)

    # Verify what actually landed on disk, decoded — see above for why the build cannot be trusted.
    chk = os.path.join(tmp, "chk.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", OUT_MP3, "-ac", "1", "-ar", "22050", chk],
                   check=True)
    got_onset, got_dur = onset_seconds(chk), wav_duration(chk)
    print(f"wrote {OUT_MP3} ({os.path.getsize(OUT_MP3)/1024:.0f} KB, "
          f"{got_dur:.2f}s, \"a\" at {got_onset:.2f}s)")
    if abs(got_onset - LEAD) > 0.05:
        raise SystemExit(f'the "a" landed at {got_onset:.2f}s, expected {LEAD:.2f}s')
    if got_dur < DURATION:
        raise SystemExit(f"mp3 is {got_dur:.2f}s, shorter than the {DURATION:.2f}s clip — it will loop")


def wav_duration(path: str) -> float:
    w = wave.open(path)
    return w.getnframes() / w.getframerate()


def scale_at(t: float) -> float:
    """Pop curve: crouched, a sharp overshoot on the "a", then a slow settle and bob."""
    if t < LEAD:
        return 0.82
    u = t - LEAD
    if u < 0.10:                       # attack — up to the overshoot
        return 0.82 + (1.18 - 0.82) * (u / 0.10)
    if u < 0.40:                       # settle back down to rest
        return 1.18 - (1.18 - 1.0) * ((u - 0.10) / 0.30)
    return 1.0 + 0.03 * math.sin(2 * math.pi * 1.2 * (u - 0.40))


def build_sprite(char: Image.Image, tmp: str):
    W, H = CANVAS
    base_h = int(H * FILL)
    base_w = max(1, int(char.width * base_h / char.height))
    frames = int(round(DURATION * FPS))
    for i in range(frames):
        s = scale_at(i / FPS)
        cw, chh = max(1, int(base_w * s)), max(1, int(base_h * s))
        f = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        # Anchored bottom-centre, which is also how the overlay sits on the photo — so the pop
        # reads as her rising out of the frame edge rather than drifting off it.
        f.alpha_composite(char.resize((cw, chh), Image.LANCZOS), ((W - cw) // 2, H - chh))
        f.save(os.path.join(tmp, f"f{i:04d}.png"))
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-framerate", str(FPS),
        "-i", os.path.join(tmp, "f%04d.png"),
        # qscale 9 rather than the prores_ks default: 3.9 MB instead of 9.6 MB for a measured
        # alpha delta of ZERO and a mean colour delta of 0.4/255. The alpha channel is what this
        # asset is for, and it comes through untouched.
        "-c:v", "prores_ks", "-profile:v", "4444", "-qscale:v", "9",
        "-pix_fmt", "yuva444p10le", OUT_MOV,
    ], check=True)
    print(f"wrote {OUT_MOV} ({os.path.getsize(OUT_MOV)/1024/1024:.1f} MB, {frames} frames @ {FPS}fps)")


def main() -> int:
    os.makedirs(os.path.join(REPO, "assets"), exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        char = fetch_pog(os.path.join(tmp, "pog.png"))
        build_audio(tmp)
        build_sprite(char, tmp)
    # Prove the alpha survived the encode: an OPAQUE .mov composites as a black box over the photo
    # and is silently skipped by the Meme Builder's alpha probe.
    fmt = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", OUT_MOV],
                         capture_output=True, text=True).stdout.strip()
    print(f"overlay pix_fmt: {fmt}")
    return 0 if fmt.startswith("yuva") else 1


if __name__ == "__main__":
    raise SystemExit(main())
