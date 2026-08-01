# `talk` — making a still picture lip-sync

Attach a photo of a face, type a line, and get back an MP4 of that face saying it.

```
talk I am the president now
talk get in the car | ana          # `| <voice>` picks who says it
```

Reachable from: AI chat (the ✨ picker → **Make it talk**, and the image action row), Telegram, and
**Discover → Meme → a photo layer → 🗣️ Make it talk**. Not exposed to the fediverse bots.

Two independent halves:

| | what | where |
|---|---|---|
| speech | edge-tts, the app's existing TTS | `app/services/tts_service.py` |
| mouth | a CPU puppet warp | `app/services/effects_service/talk.py` |

## Why a puppet warp and not Wav2Lip / SadTalker / LatentSync

* **Portability.** It is numpy + Pillow on the CPU, so it behaves identically on the CUDA box, the
  Arc/XPU box and a Docker image with no GPU. Every diffusion-shaped feature in this repo carries an
  Arc or ROCm gotcha; this carries none.
* **It never takes `GPUResourceLock`.** Nothing here competes with chat, image, music or video
  generation. See "Load, queues and the GPU" below.
* **It works on drawings.** Memes are half cartoon and the neural models are trained on video of real
  faces, so they smear on flat art. The mouth locator falls back to the anime cascade already used by
  the `blue` effect, so a hand-drawn face animates too.
* **The crude look is the point** — a Clutch Cargo jaw flap is funnier on a meme than an uncanny
  half-real mouth.

Cost of the choice: it does not form visemes. It opens and closes a mouth in time with the audio. For
a talking-meme punchline that is the whole job.

## How a frame is built

1. **Locate the face once.** `_face_geometry` returns `(cx, cy, mouth_width, angle, mouth_to_chin)`,
   all measured in the FACE's own frame — the tilt comes from the eye keypoints, and the widths are
   landmark extents along that rotated frame. A tilted face whose jaw dropped straight down the
   SCREEN would slide sideways off the chin.
2. **Turn the audio into an envelope.** `_audio_envelope` decodes to mono PCM through ffmpeg and
   takes a per-frame RMS, normalised against the clip's own 92nd percentile (not its peak — one
   plosive would otherwise mumble the whole line), gated so silence closes the mouth, then one-poled
   with a **fast attack and a slow release**. That asymmetry is what makes it read as speech. A
   second channel, the spectral centroid, widens the mouth on bright frames — a cheap stand-in for
   visemes that stops the flap looking like a metronome.
3. **Per frame:** paint the mouth cavity (dark, tooth strip, tongue) at the lip seam, then paste the
   jaw — the picture's OWN pixels, through a feathered ellipse — shifted down the face's axis. The
   jaw covers the bottom of the cavity, so what shows is exactly the gap the jaw opened.

A frame whose envelope is below `_OPEN_EPS` is the untouched original, so silence is a still.

## The landmark indices, which are measured and not documented

InsightFace's `2d106det` model ships **no semantic index table**, in the package or upstream. These
were derived by plotting all 106 points over a detected face and reading them off:

```
0-32    face contour (the chin is the point furthest down the face axis)
52-71   lip outline
```

`_LMK_LIPS` / `_LMK_CONTOUR` in `talk.py`. If a future insightface bumps the model, re-plot before
trusting them.

**Why not the 5 detection keypoints** (`kps`, which `faces._locate_mouth` uses): SCRFD's "mouth
corners" sit at **nostril height** on many faces. Measured on one: 6px high on a 36px mouth, which
puts the entire animation on the philtrum. Their WIDTH is fine; only the vertical centre is wrong.
The keypoint fallback path therefore nudges the centre down by `0.17 × mouth width`.

**Face selection** is by mouth width, not bounding-box area. On a six-face poster the boxes came out
within 0.3% of each other — a JPEG re-encode flipped which one "won" — while the mouth widths were
48.6 vs 22.0px. The mouth is what gets animated, so it is both the right key and the stable one.

## Gotchas

* **The jaw's mask has to travel with the jaw.** The mask is cropped at the SOURCE box, not the
  destination. Read it at the destination and the alpha sits still while the pixels move, so the jaw
  repaints its own original footprint — including the band the drop was supposed to uncover, which is
  where the cavity is. The symptom is a mouth that darkens slightly and never opens.
* **The cavity starts AT the lip seam, never above it.** It is composited on top of the picture, so
  an ellipse centred on the seam puts half of itself — tooth strip included — over the upper lip and
  reads as a grey smear across the philtrum.
* **PNG encoding, not rendering, was the cost.** Frames go to disk for ffmpeg, and at Pillow's
  default compression a 960x665 frame took **167ms to write and 1ms to render**: a 20s clip spent 67
  of its 68 seconds zipping temp files that are deleted moments later. `frames_to_video` /
  `frames_to_alpha_video` now write at `compress_level=1` (38ms, 10% more bytes, identical video) —
  that same fix sped up every frame-based effect. A 20s clip renders in ~16s.
* **`frames_to_video` consumes a generator lazily** at `loops=1`, because a 30s clip is 600 full-size
  frames. It used to do `frames = list(frames or [])`, and a generator is always truthy — so the
  empty-check silently passed and the whole clip was materialised.

## Load, queues and the GPU

`talk` uses **no GPU compute** and takes **no `GPUResourceLock`**. The only silicon it can touch is
the GPU's *media engine*, if `_video_encoder_candidates` picks NVENC/VAAPI for the final encode —
separate hardware from the compute cores, the same reasoning as the live-stream clamp.

It still queues, because it runs where every other meme render runs:

* **From the Meme Builder** — `POST /client/meme/apply-effect`, so it inherits that endpoint's whole
  discipline for free: the shared `_meme_slot()` render semaphore (503 when the queue won't drain),
  the per-user cooldown, and `_meme_lb_forward` busy-overflow to a peer node.
* **From AI chat / Telegram** — the ordinary `execute_command` path, exactly like `compress`,
  `removebackground` and every ffmpeg effect. ffmpeg and the per-frame Pillow work run in a thread,
  so they never block the event loop.

## Limits

`TALK_MAXDIM` 960px working edge, `TALK_FPS` 20, `TALK_MAX_DURATION` 30s, `TALK_MAX_CHARS` 400. A
mouth narrower than 12px is refused — it cannot animate into anything but mush.

## Dependencies

Nothing new. `insightface`, `opencv-python-headless`, `onnxruntime`, `numpy`, `Pillow` and `edge-tts`
are all already in `requirements.txt` for the thug/blue overlays and the existing TTS, and the
106-point weights are part of the same `buffalo_l` pack that face detection already downloads. On the
lean `GPU=nostr` image there is no opencv, and `talk` says so rather than blaming the photo.

Tests: `tests/test_talk_lipsync.py`.
