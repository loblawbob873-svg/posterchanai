"""Meme Builder — render a layered timeline (the client's edit list) into one MP4.

The client owns the EDITING (drag/resize/trim on a canvas); this owns the RENDER. It takes a JSON edit
list and turns it into a single ffmpeg invocation: a solid colour base of the project size/duration,
every layer scaled and overlaid at its own position, gated to its own time window, with per-layer
effects and audio mixed from whatever clips carry it.

Why one filtergraph instead of rendering layer-by-layer and concatenating: layers can overlap in time
and space, so they have to composite in a single pass.

Deliberately shares media_service's encoder autodetect (NVENC -> VAAPI -> libx264), so a meme is encoded
exactly like every other video this app produces. It does NOT append the outro card — a meme is often a
reply or a reaction, and a branded end card on a two-second clip is noise; wire it in if that changes.
"""
import logging
import os
import subprocess
import tempfile
from typing import Optional

from app.services import media_service

logger = logging.getLogger(__name__)

# Bounds. A meme is a short social clip, not a film — and this runs on the same shared GPU box as chat,
# so an unbounded edit list is a denial-of-service on every other feature.
MAX_LAYERS = 24
MAX_DURATION = 120.0
MAX_DIM = 2160
# Per-encoder-attempt wall clock. Was 600s, which meant a wedged ffmpeg held the user's single render slot
# for TEN MINUTES with no output and no way to start another — the "it says a render is already running but
# nothing is" symptom. A real render of a short meme is seconds; anything past this is stuck, so fail fast,
# free the slot, and tell the user what to change.
_RENDER_TIMEOUT_S = 150
DEFAULT_W, DEFAULT_H, DEFAULT_FPS = 720, 1280, 30

# Per-layer effects expressed directly in the filtergraph. Each entry is a callable taking the layer's
# resolved geometry/timing and returning ffmpeg filter chain text (applied to that layer's own stream,
# BEFORE the overlay), so effects compose with position and timing instead of fighting them.
def _fx_chain(effect: str, w: int, h: int, dur: float, fps: int) -> str:
    e = (effect or "none").strip().lower()
    if e in ("", "none"):
        return ""
    if e == "fade":                     # ease in and out of its own window
        f = min(0.5, max(0.15, dur / 6))
        return f"fade=t=in:st=0:d={f:.2f}:alpha=1,fade=t=out:st={max(0.0, dur-f):.2f}:d={f:.2f}:alpha=1"
    if e == "zoom":                     # slow Ken Burns push in
        frames = max(2, int(dur * fps))
        return (f"scale={w*2}:{h*2},zoompan=z='min(zoom+0.0015,1.35)':d={frames}:"
                f"s={w}x{h}:fps={fps},setsar=1")
    if e == "shake":
        return (f"crop={max(2,w-8)}:{max(2,h-8)}:'4+3*sin(t*22)':'4+3*cos(t*18)',"
                f"scale={w}:{h},setsar=1")
    if e == "pulse":                    # breathing scale — CONSTANT output size (see below)
        # NOT `scale=...:eval=frame`: that re-sizes the frame EVERY frame, and a stream whose dimensions keep
        # changing deadlocks the multi-input filter graph downstream (overlay/framesync). Renders wedged with
        # a 48-byte output, ~1% CPU and 232 threads parked in futex_do_wait until the timeout — verified by
        # bisecting a 6-layer project: identical project renders in 1.5s without pulse, hangs with it.
        # zoompan does the same breathing but emits a FIXED s=WxH, like the `zoom` effect above.
        return (f"scale={w*2}:{h*2},zoompan=z='1.03+0.03*sin(on/{fps}*6)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps},setsar=1")
    if e == "spin":
        return f"rotate='0.6*t':c=none:ow={w}:oh={h}"
    if e == "grayscale":
        return "hue=s=0"
    if e == "sepia":
        return "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
    if e == "blur":
        return "gblur=sigma=12"
    if e == "glow":                     # bloom: blurred bright copy screened back over the original
        return "split[a][b];[b]gblur=sigma=14,eq=brightness=0.06[bb];[a][bb]blend=all_mode=screen"
    if e == "invert":
        return "negate"
    if e == "flip":
        return "hflip"
    return ""


def _ff_colour(c: Optional[str], fallback: str = "black") -> str:
    """A hex colour we are willing to hand to ffmpeg. Anything else falls back — this string ends up in
    a command line, so it is validated rather than trusted."""
    c = (c or "").strip()
    if len(c) == 7 and c[0] == "#" and all(ch in "0123456789abcdefABCDEF" for ch in c[1:]):
        return c
    return fallback


def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):   # NaN/inf would poison the filtergraph
        return default
    return max(lo, min(hi, f))


def _drawtext(layer: dict, w: int, h: int) -> str:
    """A text layer as a drawtext filter, outlined like the `meme` command's captions so it reads on any
    background. Text is passed via textfile= by the caller (escaping arbitrary user text inline is how
    drawtext filtergraphs get broken by a stray colon or quote)."""
    size = int(_num(layer.get("size"), 8, 400, 48))
    colour = _ff_colour(layer.get("color"), "white")
    stroke = _ff_colour(layer.get("stroke"), "black")
    x = int(_num(layer.get("x"), -MAX_DIM, MAX_DIM, 0))
    y = int(_num(layer.get("y"), -MAX_DIM, MAX_DIM, 0))
    start = _num(layer.get("start"), 0, MAX_DURATION, 0)
    dur = _num(layer.get("dur"), 0.05, MAX_DURATION, 3)
    # Same font resolver the `meme` command and caption_video use, so a caption here looks identical to
    # one made anywhere else in the app (and falls back to ffmpeg's default when no font is installed).
    from app.services.effects_service._common import _meme_font_path
    font = _meme_font_path()
    fontfile = f"fontfile='{font}':" if font else ""
    return (f"drawtext={fontfile}textfile='{{TEXTFILE}}':fontsize={size}:fontcolor={colour}:"
            f"borderw={max(2, size//14)}:bordercolor={stroke}:x={x}:y={y}:"
            f"enable='between(t,{start:.3f},{start+dur:.3f})'")


def render(edit: dict, sources: dict) -> bytes:
    """Render the edit list. `sources` maps a layer's `src` key -> local file path (the caller resolves
    and fetches URLs/Blossom hashes, so this stays a pure renderer with no network of its own).

    Returns MP4 bytes. Raises ValueError on an edit list we refuse, RuntimeError if ffmpeg fails.
    """
    ffmpeg = media_service.resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available on this node")

    w = int(_num(edit.get("w"), 16, MAX_DIM, DEFAULT_W)) // 2 * 2      # even dims — h264 requires it
    h = int(_num(edit.get("h"), 16, MAX_DIM, DEFAULT_H)) // 2 * 2
    fps = int(_num(edit.get("fps"), 5, 60, DEFAULT_FPS))
    bg = _ff_colour(edit.get("bg"), "black")
    layers = [l for l in (edit.get("layers") or []) if isinstance(l, dict)]
    if not layers:
        raise ValueError("nothing to render — add at least one layer")
    if len(layers) > MAX_LAYERS:
        raise ValueError(f"too many layers ({len(layers)}); the limit is {MAX_LAYERS}")

    # Project duration: explicit, or the end of the last layer.
    ends = [_num(l.get("start"), 0, MAX_DURATION, 0) + _num(l.get("dur"), 0.05, MAX_DURATION, 3)
            for l in layers]
    duration = _num(edit.get("duration"), 0.1, MAX_DURATION, max(ends) if ends else 3.0)

    tmp = tempfile.mkdtemp(prefix="pcmeme-")
    try:
        # -filter_complex_threads 1 is the fix for the renders that WEDGED: a stuck job was sitting on 232
        # threads blocked in futex_do_wait with a 48-byte output and ~1% CPU — ffmpeg's multi-threaded filter
        # graph deadlocking on itself, not slow encoding. A meme graph is many small overlay/scale filters over
        # several looped image inputs, exactly the shape that trips it. Serialising the FILTER graph costs
        # little here (the encoder still threads via -threads) and makes the render finish instead of hanging.
        cmd = [ffmpeg, "-y", "-filter_complex_threads", "1", "-threads", "4",
               "-f", "lavfi", "-i", f"color=c={bg}:s={w}x{h}:r={fps}:d={duration:.3f}"]
        chains, audio_parts = [], []
        cur = "[0:v]"          # the running composite
        idx = 1                # ffmpeg input index (0 is the colour base)
        textfiles = []

        for n, layer in enumerate(layers):
            kind = (layer.get("type") or "").lower()
            start = _num(layer.get("start"), 0, MAX_DURATION, 0)
            dur = _num(layer.get("dur"), 0.05, MAX_DURATION, 3)
            end = start + dur

            if kind == "text":
                txt = str(layer.get("text") or "")[:500]
                if not txt.strip():
                    continue
                tf = os.path.join(tmp, f"t{n}.txt")
                with open(tf, "w", encoding="utf-8") as fh:
                    fh.write(txt)
                textfiles.append((f"[tx{n}]", _drawtext(layer, w, h).replace("{TEXTFILE}", tf)))
                continue

            path = sources.get(str(layer.get("src") or ""))
            if not path or not os.path.exists(path):
                logger.warning("[meme] layer %s has no resolved source — skipped", n)
                continue

            lw = int(_num(layer.get("w"), 2, MAX_DIM, w)) // 2 * 2
            lh = int(_num(layer.get("h"), 2, MAX_DIM, h)) // 2 * 2
            lx = int(_num(layer.get("x"), -MAX_DIM, MAX_DIM, 0))
            ly = int(_num(layer.get("y"), -MAX_DIM, MAX_DIM, 0))
            trim = _num(layer.get("trim"), 0, MAX_DURATION, 0)
            opacity = _num(layer.get("opacity"), 0.05, 1.0, 1.0)
            effect = (layer.get("effect") or "none").lower()

            if kind == "image":
                cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-i", path]
            else:
                cmd += ["-ss", f"{trim:.3f}", "-t", f"{dur:.3f}", "-i", path]

            # "contain" (default) letterboxes the source inside the layer box; "cover" scales UP until the box
            # is filled and crops the overflow — what you actually want from a "fill the canvas" background,
            # where a letterboxed image with transparent bars is not filling anything.
            if str(layer.get("fit") or "").lower() == "cover":
                chain = [f"scale={lw}:{lh}:force_original_aspect_ratio=increase",
                         f"crop={lw}:{lh}",
                         "setsar=1", f"fps={fps}", "format=rgba"]
            else:
                chain = [f"scale={lw}:{lh}:force_original_aspect_ratio=decrease",
                         f"pad={lw}:{lh}:(ow-iw)/2:(oh-ih)/2:color=black@0",
                         "setsar=1", f"fps={fps}", "format=rgba"]
            fx = _fx_chain(effect, lw, lh, dur, fps)
            if fx:
                chain.append(fx)
            if opacity < 1.0:
                chain.append(f"colorchannelmixer=aa={opacity:.3f}")
            # setpts shifts the layer to its slot on the project timeline; the overlay `enable` then
            # gates it. Both are needed: without setpts the clip plays from t=0 regardless of `start`.
            chain.append(f"setpts=PTS-STARTPTS+{start:.3f}/TB")
            chains.append(f"[{idx}:v]" + ",".join(chain) + f"[l{n}]")

            nxt = f"[v{n}]"
            chains.append(f"{cur}[l{n}]overlay=x={lx}:y={ly}:"
                          f"enable='between(t,{start:.3f},{end:.3f})':eof_action=pass{nxt}")
            cur = nxt

            if kind == "video" and layer.get("mute") is not True:
                vol = _num(layer.get("volume"), 0, 4, 1.0)
                audio_parts.append(f"[{idx}:a]adelay={int(start*1000)}|{int(start*1000)},"
                                   f"volume={vol:.2f}[a{n}]")
            idx += 1

        # Text last so captions sit above every visual layer.
        for tag, filt in textfiles:
            chains.append(f"{cur}{filt}{tag}")
            cur = tag

        chains.append(f"{cur}format=yuv420p[vout]")
        filtergraph = ";".join(chains)

        out = os.path.join(tmp, "out.mp4")
        maps = ["-map", "[vout]"]
        if audio_parts:
            # Each entry ends in its own output label ("…volume=1.00[a3]"); mix exactly those, by name.
            # Layer indexes are not contiguous (text and skipped layers leave gaps), so the labels have
            # to come from the chains themselves rather than from a range().
            labels = [p.rsplit("[", 1)[-1].rstrip("]") for p in audio_parts]
            mix_in = "".join(f"[{l}]" for l in labels)
            filtergraph += ";" + ";".join(audio_parts)
            filtergraph += f";{mix_in}amix=inputs={len(audio_parts)}:dropout_transition=0:normalize=0[aout]"
            maps += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
        else:
            maps += ["-an"]

        # Same encoder ladder every other video feature uses, so this works on the Arc, the 3060 and a
        # CPU-only node without per-node special-casing.
        encoders = media_service._video_encoder_candidates(ffmpeg)
        last_err = ""
        for enc in encoders:
            full = cmd + ["-filter_complex", filtergraph] + maps + \
                   ["-c:v", enc, "-t", f"{duration:.3f}", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
            if enc == "libx264":
                full += ["-crf", "20", "-preset", "veryfast"]
            full.append(out)
            try:
                p = subprocess.run(full, capture_output=True, timeout=_RENDER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # subprocess.run KILLS the child on timeout, so a wedged ffmpeg can't linger past this.
                raise RuntimeError(f"render timed out after {_RENDER_TIMEOUT_S}s — try fewer layers, a "
                                   f"shorter clip, or turn off per-frame effects (pulse/zoom/shake/spin)")
            if p.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                with open(out, "rb") as fh:
                    data = fh.read()
                logger.info("[meme] rendered %dx%d %.1fs, %d layers, %s -> %s",
                            w, h, duration, len(layers), enc, media_service._human_size(len(data)))
                return data
            last_err = (p.stderr or b"").decode("utf-8", "replace")[-1200:]
            logger.warning("[meme] encoder %s failed: %s", enc, last_err[-300:])
        raise RuntimeError(f"ffmpeg failed: {last_err[-500:]}")
    finally:
        try:
            for f in os.listdir(tmp):
                os.unlink(os.path.join(tmp, f))
            os.rmdir(tmp)
        except Exception:
            pass
