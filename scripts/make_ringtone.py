"""PosterChan cyberpunk ringtone: 120 BPM synthwave in A minor, 8 bars (16 s), loops cleanly.
# Regenerate: venv-unified/bin/python scripts/make_ringtone.py out.wav [voice1.wav voice2.wav]  (voices: edge-tts ja-JP-NanamiNeural, pitch +45Hz)

Am - F - C - G, two bars each. Detuned saw arpeggio (16ths) under a resonant low-pass that sweeps open,
a pumping (sidechained) saw bass on 8ths, a punchy kick on every beat, a gated clap on 2 and 4, a soft
pad, and the three-note "Pos-ter-Chan" hook (E5 - G5 - A5, the last one held with vibrato) at the top of
every chord change, through a ping-pong delay. Everything synthesized here: numpy + scipy only.
"""
import sys
import numpy as np
from scipy.signal import butter, lfilter, sosfilt

SR, BPM, BARS = 44100, 120, 8
BEAT = 60 / BPM
TOTAL = int(SR * BEAT * 4 * BARS)
rng = np.random.default_rng(7)


def hz(note):
    names = {"C": -9, "C#": -8, "D": -7, "D#": -6, "E": -5, "F": -4, "F#": -3, "G": -2, "G#": -1, "A": 0, "A#": 1, "B": 2}
    n, o = note[:-1], int(note[-1])
    return 440.0 * 2 ** ((names[n] + 12 * (o - 4)) / 12)


def saw(f, t, detune=0.0):
    out = np.zeros_like(t)
    for d in (-detune, 0, detune):
        ph = (t * f * (1 + d)) % 1.0
        out += 2 * ph - 1
    return out / 3


def env(n, a=0.005, d=0.12, s=0.6, r=0.05):
    e = np.ones(n) * s
    A, D, R = int(a * SR), int(d * SR), int(r * SR)
    A = min(A, n); e[:A] = np.linspace(0, 1, A, endpoint=False)
    D = min(D, max(0, n - A)); e[A:A + D] = np.linspace(1, s, D, endpoint=False)
    if R and n > R:
        e[-R:] *= np.linspace(1, 0, R)
    return e


def lowpass_sweep(x, f_lo, f_hi, q=0.9):
    """Time-varying resonant low-pass, applied in 1024-sample blocks (good enough, and cheap)."""
    out = np.zeros_like(x); blk = 1024; zi = None
    for i in range(0, len(x), blk):
        frac = i / len(x)
        fc = f_lo * (f_hi / f_lo) ** (0.5 - 0.5 * np.cos(2 * np.pi * frac))   # open, close, open over the loop
        sos = butter(2, min(fc, SR / 2 - 100) / (SR / 2), btype="low", output="sos")
        if zi is None:
            zi = np.zeros((sos.shape[0], 2))
        out[i:i + blk], zi = sosfilt(sos, x[i:i + blk], zi=zi)
    return out


mix_l = np.zeros(TOTAL); mix_r = np.zeros(TOTAL)
CHORDS = [["A3", "C4", "E4"], ["F3", "A3", "C4"], ["C4", "E4", "G4"], ["G3", "B3", "D4"]]
ROOTS = ["A1", "F1", "C2", "G1"]
s16 = BEAT / 4

# Arpeggio: up-down over two octaves, 16ths.
arp = np.zeros(TOTAL)
for bar in range(BARS):
    ch = CHORDS[(bar // 2) % 4]
    pattern = [ch[0], ch[1], ch[2], ch[1]]
    for k in range(16):
        f = hz(pattern[k % 4]) * (2 if (k // 4) % 2 else 1)
        start = int((bar * 4 * BEAT + k * s16) * SR); n = int(s16 * SR)
        t = np.arange(n) / SR
        arp[start:start + n] += saw(f, t, 0.006) * env(n, 0.002, 0.08, 0.35, 0.02)
arp = lowpass_sweep(arp, 700, 5200) * 0.32

# Bass: 8ths on the root, sidechain-pumped against the kick.
bass = np.zeros(TOTAL)
for bar in range(BARS):
    f = hz(ROOTS[(bar // 2) % 4])
    for k in range(8):
        start = int((bar * 4 * BEAT + k * BEAT / 2) * SR); n = int(BEAT / 2 * SR)
        t = np.arange(n) / SR
        bass[start:start + n] += saw(f, t, 0.003) * env(n, 0.003, 0.15, 0.7, 0.03)
b, a = butter(2, 420 / (SR / 2), btype="low"); bass = lfilter(b, a, bass)
pump = np.ones(TOTAL)
for beat in range(BARS * 4):
    s = int(beat * BEAT * SR); n = int(BEAT * SR)
    pump[s:s + n] = 0.25 + 0.75 * (1 - np.exp(-np.arange(n) / (0.09 * SR)))
bass *= pump * 0.55

# Kick: sine with a fast pitch drop.
kick = np.zeros(TOTAL); n = int(0.35 * SR); t = np.arange(n) / SR
k_one = np.sin(2 * np.pi * (45 * t + (150 - 45) * (1 - np.exp(-t * 40)) / 40)) * np.exp(-t * 9)
for beat in range(BARS * 4):
    s = int(beat * BEAT * SR); kick[s:s + n] += k_one[:max(0, min(n, TOTAL - s))]
kick *= 0.9

# Clap on 2 and 4: band-passed noise, three quick hits then a tail.
clap = np.zeros(TOTAL); n = int(0.25 * SR)
noise = rng.standard_normal(n)
b, a = butter(2, [900 / (SR / 2), 4200 / (SR / 2)], btype="band"); noise = lfilter(b, a, noise)
shape = np.exp(-np.arange(n) / (0.06 * SR))
for off in (0, int(0.012 * SR), int(0.024 * SR)):
    shape[off:off + int(0.01 * SR)] += 0.8
c_one = noise * shape
for beat in range(BARS * 4):
    if beat % 2 == 1:
        s = int(beat * BEAT * SR); m = max(0, min(n, TOTAL - s)); clap[s:s + m] += c_one[:m]
clap *= 0.35

# Hi-hats on the off 8ths.
hat = np.zeros(TOTAL); n = int(0.05 * SR)
hn = rng.standard_normal(n); b, a = butter(2, 7000 / (SR / 2), btype="high"); hn = lfilter(b, a, hn) * np.exp(-np.arange(n) / (0.012 * SR))
for e8 in range(BARS * 8):
    if e8 % 2 == 1:
        s = int(e8 * BEAT / 2 * SR); m = max(0, min(n, TOTAL - s)); hat[s:s + m] += hn[:m]
hat *= 0.12

# Pad: soft chord, slow attack, low-passed.
pad = np.zeros(TOTAL)
for bar in range(0, BARS, 2):
    s = int(bar * 4 * BEAT * SR); n = int(8 * BEAT * SR); t = np.arange(n) / SR
    for note in CHORDS[(bar // 2) % 4]:
        pad[s:s + n] += saw(hz(note), t, 0.01) * env(n, 0.6, 0.5, 0.8, 0.4)
b, a = butter(2, 1500 / (SR / 2), btype="low"); pad = lfilter(b, a, pad) * 0.11

# The hook: "Pos - ter - Chan" (E5 G5 A5), at every chord change, the last note held with vibrato.
hook = np.zeros(TOTAL)
for bar in range(0, BARS, 2):
    base = bar * 4 * BEAT
    for i, (note, at, dur) in enumerate((("E5", 0, 0.5), ("G5", 0.5, 0.5), ("A5", 1.0, 1.5))):
        s = int((base + at * BEAT) * SR); n = int(dur * BEAT * SR); t = np.arange(n) / SR
        vib = 1 + (0.006 * np.sin(2 * np.pi * 5.5 * t) * np.clip(t / 0.25, 0, 1) if i == 2 else 0)
        ph = np.cumsum(hz(note) * vib) / SR
        tone = (2 * (ph % 1.0) - 1) * 0.6 + np.sign(np.sin(2 * np.pi * ph)) * 0.4
        hook[s:s + n] += tone * env(n, 0.01, 0.1, 0.7, 0.08)
b, a = butter(2, 3200 / (SR / 2), btype="low"); hook = lfilter(b, a, hook) * 0.22

# Ping-pong delay on the hook and the arp (dotted 8th).
d = int(0.75 * BEAT * SR)
send = hook + arp * 0.35
dl = np.zeros(TOTAL); dr = np.zeros(TOTAL)
for rep, g in enumerate((0.45, 0.3, 0.18, 0.1)):
    off = d * (rep + 1)
    tgt = dl if rep % 2 == 0 else dr
    tgt[off:] += send[:TOTAL - off] * g

# THE VOICE ("would be cool if it was a japanese anime voice going Ring Ring"): 「リンリン！」 answering the
# hook on the second bar of every chord, 「リンリーン♪」 on alternate ones, with the music ducked under it so it
# cuts through a phone speaker. Optional: argv[2] / argv[3] are mono 44.1 kHz WAVs of the two lines.
voice = np.zeros(TOTAL)
def _load(path):
    import wave as _w
    with _w.open(path) as f:
        a = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype(float) / 32768
        return a if f.getnchannels() == 1 else a.reshape(-1, f.getnchannels()).mean(1)
lines = [_load(p) for p in sys.argv[2:4]]
if lines:
    for k, bar in enumerate((1, 3, 5, 7)):
        v = lines[k % len(lines)] if len(lines) > 1 and k % 2 else lines[0]
        s = int(bar * 4 * BEAT * SR); m = min(len(v), TOTAL - s)
        voice[s:s + m] += v[:m] / (np.max(np.abs(v)) + 1e-9)
    duck = np.ones(TOTAL); env_v = np.convolve(np.abs(voice) > 0.02, np.ones(int(0.08 * SR)) / int(0.08 * SR), "same")
    duck -= 0.5 * np.clip(env_v * 3, 0, 1)
    arp *= duck; pad *= duck; hook *= duck; bass *= 0.6 + 0.4 * duck
    vd = int(0.5 * BEAT * SR); echo = np.zeros(TOTAL); echo[vd:] = voice[:TOTAL - vd] * 0.28
    voice = voice * 0.85 + echo

dry = arp + bass + kick + clap + hat + pad + hook + voice
mix_l = dry + dl * 0.8 + dr * 0.2
mix_r = dry + dr * 0.8 + dl * 0.2
st = np.stack([mix_l, mix_r], 1)
st = np.tanh(st * 1.4) / np.tanh(1.4)                       # gentle saturation / glue
st /= np.max(np.abs(st)) / 0.95
fade = int(0.02 * SR); st[:fade] *= np.linspace(0, 1, fade)[:, None]; st[-fade:] *= np.linspace(1, 0, fade)[:, None]

import wave
out = sys.argv[1]
with wave.open(out, "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((st * 32767).astype("<i2").tobytes())
print("wrote", out, round(TOTAL / SR, 2), "s")
