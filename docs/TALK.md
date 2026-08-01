# `talk` — making a still picture lip-sync

Attach a photo of a face **and a few seconds of a voice**, type a line, and get back an MP4 of that
face saying it **in that voice**.

```
talk I am the president now        # with a photo AND a voice clip attached
```

Reachable from: AI chat (the ✨ picker → **Make it talk**, and the image action row), Telegram, and
**Discover → Meme → a photo layer → 🗣️ Make it talk**. Not exposed to the fediverse bots.

Two halves, and the split is the whole design:

| | what | where | cost |
|---|---|---|---|
| speech | the **cloned-voice model**, the same one `voice` uses | `voice_factory` → `voice_local` | the node's **GPU**, ~10x realtime |
| mouth | a CPU puppet warp | `app/services/effects_service/talk.py` | CPU, ~16s for a 20s clip |

`talk` is deliberately **not** edge-tts. edge-tts is what `narrate` uses — cloud, instant, and a
stock voice. Cloning is the point of the feature, so the speech goes through `voice_factory`, which
already owns the `GPUResourceLock`, `prepare_for_voice`'s VRAM swap, the round-robin over
`chat_server_urls` and the busy probe. Nothing about the GPU is reimplemented here.

`talk.py` itself takes a picture and *a path to already-generated audio*. It knows nothing about
where the speech came from — which is exactly what keeps the GPU discipline in one place.

## Where the voice comes from

| surface | reference | why |
|---|---|---|
| AI chat | a second **attachment** (the ✨ picker takes both files at once) | same as `voice`: the voice library is client-side |
| Meme Builder | **your saved voices**, via `PC.openVoiceStudio` | the library, recorder, queue notice and length estimate already exist there |

### Telegram is NOT finished — known gap

`talk` is matched on Telegram (so it can never fall through to the LLM) but it cannot currently
succeed there, and this is a limitation of the transport, not a bug to hunt:

* Telegram cannot put a photo **and** an audio clip in one message — a media group is photos/videos
  only — so the two files can never arrive together.
* The Telegram handler does not download `message.voice` / `message.audio` **at all** today (see
  `messages.py`, which reads only `photo`, `document` and `video`). That is pre-existing, and it
  means the `voice` command's own "reply to a voice note with `voice <text>`" docstring does not
  actually hold on Telegram either.

The fix is an interactive two-step flow like `clip`'s: send the photo with `talk <what to say>`, the
bot ForceReply-prompts for a voice note, then renders. That is not built. Until it is, Telegram users
get the "attach a voice clip" reply, which is honest but unsatisfiable there — so treat `talk` as
**web UI only** in practice.

The Meme Builder button borrows AI Chat's voice studio with an `onTake`, exactly as "Add a voice
line" does — only the ENDING differs. The take is uploaded to the user's own drive and its URL handed
to `POST /client/meme/talk`, which animates the layer's face. So the speech is generated through
`/client/voice/speak` (GPU lock, per-user cooldown, LB) and the render through the meme queue; there
is no second endpoint that generates speech.

## Anime and other flat art — and why you place the mouth

**Every face model here was trained on photographs.** InsightFace will happily *detect* an anime
face — its box and its two eye keypoints land correctly — and then put the mouth landmarks on the
chin and a cheek. Measured on a Chainsaw Man still: a mouth 1.7× too wide and 16px too low. A
confident wrong answer is worse than none, because it means the anime fallback is never reached.
The cascade's own estimate is no better for this: its 0.42×face-width mouth belongs to the `blue`
effect, which paints *around* the mouth and wants to be generous — used for lip-sync it is roughly
three times too wide, and that was the "doesn't work on anime at all" report.

So the Meme Builder asks. **🗣️ Make it talk** opens a placement control *before* spending a minute
of GPU on the voice: the picture with a draggable marker and a width slider, seeded from the
server's detector (`POST /client/meme/face`, CPU-only, no render slot — it runs while you decide).
On a photo the seed is already right and you just press **Use it**. On anime, a 3D render, a
mascot, a logo with a face, or one face in a crowd, you drag it. The placement is **normalised**
(fractions of the image), so it survives every resize between the browser and the renderer.

The same dialog asks **Photo or Drawing** — because that picks the RENDERER, and it is a judgement
about the artwork that the person looking at it can make and the detector cannot:

| | operation | why |
|---|---|---|
| Photo | **warp** the real jaw | keeps the face's own detail |
| Drawing / anime | **redraw** the mouth | a cel mouth is an ink stroke; sliding it duplicates and smears it |

Detection is still the default everywhere there is no picker (chat, Telegram), and `add_talk`
auto-detects when `mouth=` is omitted.

Server-side the placement is **clamped**, not merely parsed (`_clean_mouth`): it is untrusted input
that becomes ellipse dimensions inside a 600-iteration render loop, and `w` is what every length
scales off — an unclamped `0.9` would build canvases the size of the picture, per frame.

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
* **A cut-out layer must stay cut out, and that forces a SILENT clip.** A Meme Builder layer
  composites, so a background-removed photo has transparency that matters. MP4 has no alpha channel
  at all, so rendering one turns that layer into a **black rectangle with the subject pasted on
  top**. The transparent form has to be a VP9-alpha WebM — and that form has to be silent, because
  an audio stream inside one corrupts the alpha on this ffmpeg (`media_service._ALPHA_VCODEC`, which
  is why *every* alpha layer in the builder is silent). So `add_talk(keep_alpha=True)` returns
  `(webm, "video/webm")` when the source really uses transparency, the endpoint reports `alpha:true`,
  and the client puts the spoken line on the timeline as its **own audio layer** aligned to the clip.
  Chat and Telegram leave `keep_alpha` off and take the MP4: a reply has to be one self-contained
  file, and a transparent clip with no sound is the feature failing quietly.
  **ffprobe will report `pix_fmt=yuv420p` on that WebM and the alpha is still there** — VP9 carries it
  as a separate track, so you only see it by decoding with `-c:v libvpx-vp9` (which
  `meme_builder_service` does for every `.webm` layer). Don't "fix" a working file on ffprobe's say-so.
* **The reference clip goes in a SUBDIRECTORY of the temp dir.** It keeps the upload's own filename,
  so written beside the normalised output an attachment that happens to be called `ref.wav` *is* the
  output path, and ffmpeg refuses with "cannot edit existing files in-place" on a clip that is
  otherwise perfect. (This bit `voice` too — the helper is now shared, so it is fixed for both.)
* **PNG encoding, not rendering, was the cost.** Frames go to disk for ffmpeg, and at Pillow's
  default compression a 960x665 frame took **167ms to write and 1ms to render**: a 20s clip spent 67
  of its 68 seconds zipping temp files that are deleted moments later. `frames_to_video` /
  `frames_to_alpha_video` now write at `compress_level=1` (38ms, 10% more bytes, identical video) —
  that same fix sped up every frame-based effect. A 20s clip renders in ~16s.
* **`frames_to_video` consumes a generator lazily** at `loops=1`, because a 30s clip is 600 full-size
  frames. It used to do `frames = list(frames or [])`, and a generator is always truthy — so the
  empty-check silently passed and the whole clip was materialised.

## Load, queues and the GPU

Two different queues, because the two halves cost different things.

**The speech is GPU work** and goes through `voice_factory` like every other voice request:
`GPUResourceLock` (so chat, image, music, video and voice all serialise on one lock),
`prepare_for_voice` to swap other models out of VRAM, round-robin over the unified `chat_server_urls`
node list, and a busy probe that demotes a node known to be working. `queue_depth()` is what the
studio shows while you wait. None of that is new code — `talk` calls the same function `voice` does.

**The mouth is CPU work** and runs where every other meme render runs:

* **From the Meme Builder** — `POST /client/meme/talk`, which takes the same `_meme_slot()` render
  semaphore (503 when the queue won't drain), the same per-user cooldown, and the same
  `_meme_lb_forward` overflow to a peer node as `/meme/apply-effect`. It takes **no `GPUResourceLock`
  and does no GPU compute**; the only silicon it can touch is the GPU's *media engine*, if
  `_video_encoder_candidates` picks NVENC/VAAPI for the final encode — separate hardware from the
  compute cores, the same reasoning as the live-stream clamp.
* **From AI chat / Telegram** — the ordinary `execute_command` path, exactly like `compress` and
  every ffmpeg effect. ffmpeg and the per-frame Pillow work run in a thread, so they never block the
  event loop.

## Limits

`TALK_MAXDIM` 960px working edge, `TALK_FPS` 20, `TALK_MAX_DURATION` 30s, `TALK_MAX_CHARS` 400. A
mouth narrower than 12px is refused — it cannot animate into anything but mush.

`TALK_MAX_CHARS` is the renderer's bound, not `voice_max_chars` (default 800): a line long enough to
run past `TALK_MAX_DURATION` would have its video cut mid-sentence, so it is refused up front instead.

Speech needs `voice_enabled` (Admin → Voice) and `./install.sh --voice`; `talk` says which of those
is missing rather than failing opaquely.

## Dependencies

Nothing new. `insightface`, `opencv-python-headless`, `onnxruntime`, `numpy` and `Pillow` are already
in `requirements.txt` for the thug/blue overlays, the 106-point weights are part of the same
`buffalo_l` pack that face detection already downloads, and the voice model is the existing
`./install.sh --voice`. On the lean `GPU=nostr` image there is no opencv, and `talk` says so rather
than blaming the photo.

Tests: `tests/test_talk_lipsync.py`.
