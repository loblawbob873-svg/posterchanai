"""PosterChan notification sound: a cute anime cyberpunk alert, ~1.6 s.
# Regenerate: venv-unified/bin/python scripts/make_notification_sound.py out.wav [voice.wav]
#             venv-unified/bin/python scripts/make_notification_sound.py --chime out.wav
#   --chime: the everyday arrival chime -- the same zap and sparkle, shorter (0.85 s) and with no voice,
#   because it plays for every like and reply and a voice forty times a day is not cute any more.
#   voice: edge-tts ja-JP-NanamiNeural, pitch +45Hz, rate +10%, 「ピコーン♪」 -> mono 44.1 kHz WAV
#   (the same voice as the ringtone's 「リンリン！」, scripts/make_ringtone.py)

A glitchy cyberpunk "zap" (a bit-crushed square chirping down through a noise burst), then a bright sparkle
arpeggio (A5 C#6 E6 A6, FM bells, 55 ms apart) through a ping-pong delay, and the voice landing on the last
bell. Short and front-loaded, the way a notification has to be: the first 150 ms already say "PosterChan".
Everything synthesized here: numpy + scipy only.
"""
import sys
import wave

import numpy as np
from scipy.signal import butter, lfilter

SR = 44100
CHIME = "--chime" in sys.argv
if CHIME:
    sys.argv.remove("--chime")
TOTAL = int((0.85 if CHIME else 1.6) * SR)
rng = np.random.default_rng(11)


def hz(note):
    names = {"C": -9, "C#": -8, "D": -7, "D#": -6, "E": -5, "F": -4, "F#": -3, "G": -2, "G#": -1, "A": 0, "A#": 1, "B": 2}
    n, o = note[:-1], int(note[-1])
    return 440.0 * 2 ** ((names[n] + 12 * (o - 4)) / 12)


def place(dst, src, at):
    s = int(at * SR); m = max(0, min(len(src), len(dst) - s)); dst[s:s + m] += src[:m]


# 1. The zap: a square wave sweeping 2.4 kHz -> 300 Hz in 110 ms, bit-crushed to 4 bits and sample-held,
#    under a short band-passed noise burst. The "cyberpunk glitch".
n = int(0.11 * SR); t = np.arange(n) / SR
f = 300 + 2100 * np.exp(-t * 30)
sq = np.sign(np.sin(2 * np.pi * np.cumsum(f) / SR))
sq = np.round(sq * 7) / 7                                   # 4-bit
hold = 6; sq = np.repeat(sq[::hold], hold)[:n]              # sample-and-hold "digital" grit
noise = rng.standard_normal(n); b, a = butter(2, [2500 / (SR / 2), 9000 / (SR / 2)], btype="band")
zap = (sq * 0.5 + lfilter(b, a, noise) * 0.35) * np.exp(-t * 22)
zapl = np.zeros(TOTAL); place(zapl, zap * (0.35 if CHIME else 0.55), 0.0)

# 2. The sparkle: FM bells, a major arpeggio climbing an octave -- the cute half.
def bell(freq, dur, bright=2.2):
    m = int(dur * SR); tt = np.arange(m) / SR
    mod = bright * np.exp(-tt * 9) * np.sin(2 * np.pi * freq * 3.5 * tt)
    return np.sin(2 * np.pi * freq * tt + mod) * np.exp(-tt * 6.5) * np.clip(tt / 0.003, 0, 1)

spark = np.zeros(TOTAL)
NOTES = ("E6", "B6") if CHIME else ("A5", "C#6", "E6", "A6")        # the chime: a two-note "pi-pon", up a fifth
for i, note in enumerate(NOTES):
    place(spark, bell(hz(note), 0.9 - i * 0.1) * (0.5 + 0.12 * i), (0.07 if CHIME else 0.10) + i * (0.09 if CHIME else 0.055))
# a held top shimmer under the voice
m = int(0.9 * SR); tt = np.arange(m) / SR
shimmer = (np.sin(2 * np.pi * hz("E7") * tt) + 0.5 * np.sin(2 * np.pi * hz("A7") * tt)) * np.exp(-tt * 4) * 0.08
place(spark, shimmer * (1 + 0.3 * np.sin(2 * np.pi * 9 * tt)) * (0.6 if CHIME else 1), 0.18 if CHIME else 0.30)

# 3. The voice: 「ピコーン♪」, trimmed to where it speaks, landing on the last bell.
voice = np.zeros(TOTAL)
if len(sys.argv) > 2 and not CHIME:
    with wave.open(sys.argv[2]) as w:
        v = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32768
        if w.getnchannels() > 1:
            v = v.reshape(-1, w.getnchannels()).mean(1)
    on = np.where(np.abs(v) > 0.02)[0]
    if len(on):
        v = v[max(0, on[0] - int(0.01 * SR)):on[-1] + int(0.04 * SR)]
    v = v / (np.max(np.abs(v)) + 1e-9)
    b, a = butter(2, 180 / (SR / 2), btype="high"); v = lfilter(b, a, v)        # thin, bright, phone-speaker friendly
    place(voice, v * 0.9, 0.27)

# Ping-pong delay on the sparkle and the voice (an eighth at 150 BPM).
d = int((0.14 if CHIME else 0.2) * SR)
send = spark + voice * 0.4
dl = np.zeros(TOTAL); dr = np.zeros(TOTAL)
for rep, g in enumerate((0.35, 0.2, 0.1)):
    off = d * (rep + 1)
    (dl if rep % 2 == 0 else dr)[off:] += send[:TOTAL - off] * g

dry = zapl + spark + voice
st = np.stack([dry + dl * 0.8 + dr * 0.2, dry + dr * 0.8 + dl * 0.2], 1)
st = np.tanh(st * 1.3) / np.tanh(1.3)
st /= np.max(np.abs(st)) / 0.95
fade = int((0.2 if CHIME else 0.12) * SR); st[-fade:] *= np.linspace(1, 0, fade)[:, None]
st[:int(0.002 * SR)] *= np.linspace(0, 1, int(0.002 * SR))[:, None]

with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((st * 32767).astype("<i2").tobytes())
print("wrote", sys.argv[1], round(TOTAL / SR, 2), "s")
