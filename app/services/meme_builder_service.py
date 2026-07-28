"""Meme Builder — render a layered timeline (the client's edit list) into one MP4.

The client owns the EDITING (drag/resize/trim on a canvas); this owns the RENDER. It takes a JSON edit
list and turns it into a single ffmpeg invocation: a solid colour base of the project size/duration,
every layer scaled and overlaid at its own position, gated to its own time window, with per-layer
effects and audio mixed from whatever clips carry it. A layer can also be pure audio (a music bed) —
it contributes nothing to the composite, only a track to the mix.

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
    # Centring must be done by ffmpeg, not by the client guessing a pixel x. The browser and ffmpeg lay text
    # out with different fonts AND the preview wraps at 92% while drawtext never wraps at all — so a caption
    # the client measured as centred rendered as one long line starting near the left edge. `(w-text_w)/2`
    # uses drawtext's OWN measurement of the text it is about to draw, so it is centred by construction.
    # Horizontal placement is anchored on the caption's CENTRE, not its left edge, whenever the client can
    # measure one (`cx`). The browser and ffmpeg never agree on a string's width — different faces, hinting
    # and synthetic bolding — so an x computed for the preview's width lands the rendered text left or right
    # of where you put it, and goes NEGATIVE for a caption wider than the canvas. Centre-anchoring cancels
    # that: ffmpeg subtracts HALF OF ITS OWN text_w, so only the centre point has to be agreed on.
    if str(layer.get("align") or "").lower() == "center":
        x_expr = "(w-text_w)/2"
    else:
        cx = layer.get("cx")
        if cx is None:
            x_expr = str(x)
        else:
            x_expr = f"({int(_num(cx, -MAX_DIM, MAX_DIM, 0))})-text_w/2"
    start = _num(layer.get("start"), 0, MAX_DURATION, 0)
    dur = _num(layer.get("dur"), 0.05, MAX_DURATION, 3)
    # Same font resolver the `meme` command and caption_video use, so a caption here looks identical to
    # one made anywhere else in the app (and falls back to ffmpeg's default when no font is installed).
    from app.services.effects_service._common import _meme_font_path
    font = _meme_font_path()
    fontfile = f"fontfile='{font}':" if font else ""
    end = start + dur
    # Effects on a TEXT layer used to be silently dropped: text is drawn with drawtext onto the composite,
    # so it never passes through _fx_chain the way media streams do — you could choose "Fade" on a caption
    # and nothing happened. Fade IS expressible as drawtext's own alpha ramp, so honour it here.
    alpha = ""
    if (layer.get("effect") or "").strip().lower() == "fade":
        f = min(0.5, max(0.15, dur / 6))
        alpha = (f":alpha='if(lt(t,{start:.3f}),0,"
                 f"if(lt(t,{start + f:.3f}),(t-{start:.3f})/{f:.3f},"
                 f"if(lt(t,{end - f:.3f}),1,max(0,({end:.3f}-t)/{f:.3f}))))'")
    # ONE drawtext PER LINE (see the caller): this build of ffmpeg honours a newline in the textfile
    # as a break AND draws a notdef box for the LF itself — "line one[]" — so the control character
    # must never reach drawtext at all. `_line_dy` shifts each line down by a line height.
    y_off = int(_num(layer.get("_line_dy"), 0, 200, 0)) * int(round(size * 1.18))
    y_expr = f"{y + y_off}"
    return (f"drawtext={fontfile}textfile='{{TEXTFILE}}':fontsize={size}:fontcolor={colour}:"
            f"borderw={max(2, size//14)}:bordercolor={stroke}:x={x_expr}:y={y_expr}{alpha}:"
            f"enable='between(t,{start:.3f},{end:.3f})'")



def sound_names() -> list:
    """Names of the AI-chat SOUND effects (`curb`, `fahh`, `sopranos`, …), discovered from the
    effects_service `_<name>_audio_path()` helpers so this list can never drift from what actually
    resolves to a file."""
    import re as _re, importlib, pkgutil
    import app.services.effects_service as _pkg
    out = []
    # Scan the SUBMODULES: `from .audio2 import *` does not re-export underscore-prefixed names, so the
    # helpers are not attributes of the package itself.
    for mod in pkgutil.iter_modules(_pkg.__path__):
        try:
            m = importlib.import_module(f"app.services.effects_service.{mod.name}")
        except Exception:
            continue
        for attr in dir(m):
            hit = _re.fullmatch(r"_([a-z0-9]+)_audio_path", attr)
            if hit:
                out.append(hit.group(1))
    return sorted(set(out))


# ---- Full-effect ALPHA layers -------------------------------------------------------------------
# A handful of the effects_service overlays can be rendered on a TRANSPARENT canvas (see
# media_service.frames_to_alpha_video / still_to_alpha_video) and dropped onto the Meme Builder
# timeline as a compositable video layer over whatever is beneath. Each carries its own sound where
# the effect has one. The client picks from alpha_effect_catalog(); the render runs via
# render_alpha_effect(). Nothing here appends the branded outro — that belongs on the FINAL meme.
_ALPHA_CHARACTERS = [
    # (name, label) — the character-overlay effects worth having as a transparent layer. `shrug` is
    # intentionally NOT here: it is exposed separately below because it carries audio and its own pose.
    # `lookingaway` is the two-panel monkey-puppet meme, not a pose — it is rendered by
    # render_lookingaway_alpha, so the label promises the turn that the clip actually performs.
    # (`anyways` is the original command name and still resolves; see COMMAND_ALIASES.)
    ("carl", "🫵 Carl"), ("soyjack", "😮 Soyjaks pointing"), ("lookingaway", "🙈 Looking away (turns to camera)"),
    ("would", "Would (old man)"), ("theraped", "Pointing (anime)"),
]


def alpha_effect_catalog() -> list:
    """The full effects that can be added as a transparent LAYER, filtered to what actually resolves on
    THIS node (so the client never offers a broken pick). Each entry:
      {"name": str, "label": str, "audio": bool, "dur": float}
    `audio` reports whether the layer will carry sound, so the picker can hint it. `dur` is the clip's
    natural length, so the caller can give the timeline layer a slot the clip actually fills — it lives
    HERE, next to the renderer that decides it, rather than as a ladder of magic numbers at the caller.
    Mirrors sound_names()/_sound_path(): discovered/validated against real files, never a hard-coded
    list that can drift from the installed assets."""
    from app.services.effects_service import nakedman as _nm, character as _ch
    out = []
    # nakedman is drawn procedurally, so it is always available; its audio is a bundled asset.
    out.append({"name": "nakedman", "label": "🍆 Naked man (dancing)",
                "audio": bool(_nm._nakedman_audio_path()), "dur": 8.0})
    # shrug needs its pose art; audio is a bundled asset.
    if _ch._character_path("shrug"):
        out.append({"name": "shrug", "label": "🤷 Shrug",
                    "audio": bool(_ch._shrug_audio_path()), "dur": 2.7})
    for key, label in _ALPHA_CHARACTERS:
        if _ch._character_path(key):
            # A still pose holds for as long as you like; the two-panel turn has its own beat.
            out.append({"name": key, "label": f"🧍 {label}", "audio": False,
                        "dur": _ch.LOOKINGAWAY_ALPHA_DUR if key == "lookingaway" else 6.0})
    return out


# Names a caller may still be holding for an effect the catalogue now lists under its real name. A
# client caches the catalogue, and `anyways` is what people have typed for months, so both have to
# keep resolving — mirrors CommandService.COMMAND_ALIASES for the command path.
_ALPHA_ALIASES = {"anyways": "lookingaway", "lookaway": "lookingaway"}


def canonical_alpha_effect(name: str) -> str:
    """An effect name resolved to the one the catalogue actually lists."""
    n = (name or "").strip().lower()
    return _ALPHA_ALIASES.get(n, n)


def render_alpha_effect(name: str, dur: float = None) -> tuple:
    """Render one alpha-capable effect to a transparent .mov (ProRes 4444, see media_service). Returns
    (mov_bytes, has_audio). Raises ValueError for a name not in the catalogue, RuntimeError on a render
    failure. `dur` (seconds) is an optional length hint honoured by the effects that support it."""
    from app.services.effects_service import nakedman as _nm, character as _ch
    name = canonical_alpha_effect(name)
    allowed = {e["name"]: e for e in alpha_effect_catalog()}
    meta = allowed.get(name)
    if not meta:
        raise ValueError(f"unknown effect: {name}")
    if name == "nakedman":
        data = _nm.render_nakedman_alpha(dur=dur or 8.0)
    elif name == "shrug":
        data = _ch.render_shrug_alpha(dur=dur)
    elif name == "lookingaway":
        # NOT render_character_alpha: that resolves the name to the one-panel anyways.png, which is
        # half the meme — the puppet mid-side-eye, with no turn. See render_lookingaway_alpha.
        data = _ch.render_lookingaway_alpha(dur=dur)
    else:
        data = _ch.render_character_alpha(name, dur=dur or 6.0)
    return data, bool(meta["audio"])


def _sound_path(name: str) -> str:
    """Absolute path to a named sound effect's audio file, or "" if it isn't installed."""
    name = (name or "").strip().lower()
    if not name or not name.isalnum():
        return ""
    try:
        import importlib, pkgutil
        import app.services.effects_service as _pkg
        for mod in pkgutil.iter_modules(_pkg.__path__):
            m = importlib.import_module(f"app.services.effects_service.{mod.name}")
            fn = getattr(m, f"_{name}_audio_path", None)
            if callable(fn):
                return fn() or ""
        return ""
    except Exception:
        return ""



def _has_audio(path: str) -> bool:
    """True if the file actually carries an audio stream. Referencing [n:a] for an input that has none is a
    HARD ffmpeg error, not a no-op — so a silent MP4 (a screen recording, a GIF conversion, or a meme this
    very tool exported with -an) failed the whole render with a 500 instead of just being silent."""
    try:
        p = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "a:0",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                           capture_output=True, timeout=20)
        return b"audio" in (p.stdout or b"")
    except Exception:
        return False


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

    # Project duration: explicit, or the end of the last layer. AUDIO layers are excluded on purpose —
    # a three-minute song dropped onto a six-second meme must not stretch the video to three minutes
    # (it would also slam straight into MAX_DURATION). Music is truncated at the end of the timeline
    # instead; see the audio branch below.
    ends = [_num(l.get("start"), 0, MAX_DURATION, 0) + _num(l.get("dur"), 0.05, MAX_DURATION, 3)
            for l in layers if (l.get("type") or "").lower() != "audio"]
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
                # Split on newlines and emit one drawtext per line — a raw LF in the textfile renders
                # as a tofu box in this ffmpeg build (verified: "line one[]" / "line two"). Blank lines
                # still consume a line height so the spacing the user typed is preserved.
                _lines = txt.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                def _chain(lay):
                    parts = []
                    for _i, _ln in enumerate(_lines):
                        if not _ln.strip():
                            continue
                        _tf = os.path.join(tmp, f"t{n}_{_i}.txt")
                        with open(_tf, "w", encoding="utf-8") as _fh:
                            _fh.write(_ln)
                        _l = dict(lay); _l["_line_dy"] = _i
                        parts.append(_drawtext(_l, w, h).replace("{TEXTFILE}", _tf))
                    return ",".join(parts)
                dt = _chain(layer)
                if not dt:
                    continue
                # The own-stream path draws onto a canvas whose OWN timeline runs 0..dur (the shift to the
                # project timeline happens later via setpts), so drawtext's enable window and fade ramp must
                # be layer-local. Using the project-absolute start there meant any caption with start>0 was
                # enabled for frames that canvas never has — the text simply never appeared.
                _local = dict(layer); _local["start"] = 0.0
                dt_local = _chain(_local)
                fxn = (layer.get("effect") or "none").strip().lower()
                if fxn in ("", "none", "fade"):
                    # Cheap path: draw straight onto the composite (and fade is handled inside _drawtext as
                    # an alpha ramp). Keeps captions above every visual layer, which is what you want by default.
                    textfiles.append((f"[tx{n}]", dt))
                    # This path `continue`s WITHOUT adding a video input, so its sound has to be handled here
                    # too — otherwise a caption with a sound but no effect silently loses the sound (it did).
                    snd0 = _sound_path(layer.get("sound"))
                    if snd0:
                        cmd += ["-t", f"{dur:.3f}", "-i", snd0]
                        svol0 = _num(layer.get("soundVolume"), 0, 4, 1.0)
                        audio_parts.append(f"[{idx}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,"
                                           f"loudnorm=I=-16:TP=-1.5:LRA=11,"
                                           f"adelay={int(start*1000)}|{int(start*1000)},volume={svol0:.2f}[s{n}]")
                        idx += 1
                    continue
                # Any OTHER effect needs the caption to be its OWN stream — a filter like blur/spin/zoom acts
                # on a video frame, and text drawn onto the composite has no frame of its own to transform.
                # So: a transparent canvas the size of the project, the text drawn on it, then the effect, then
                # overlaid. That makes every effect in the menu work on a caption, not just fade.
                cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                        "-i", f"color=c=black:s={w}x{h}:r={fps}"]
                # colorchannelmixer=aa=0 FORCES the canvas transparent. `color=c=black@0` does NOT survive
                # negotiation on ffmpeg 7.x here — it comes out fully opaque, so the full-frame overlay
                # painted BLACK over every layer beneath the caption. Zeroing alpha explicitly is reliable.
                # The caption is drawn AFTER, so its own glyphs keep their alpha.
                tchain = ["format=rgba", "colorchannelmixer=aa=0", dt_local]
                tfx = _fx_chain(fxn, w, h, dur, fps)
                if tfx:
                    tchain.append(tfx)
                tchain.append(f"setpts=PTS-STARTPTS+{start:.3f}/TB")
                chains.append(f"[{idx}:v]" + ",".join(tchain) + f"[l{n}]")
                tnxt = f"[v{n}]"
                chains.append(f"{cur}[l{n}]overlay=x=0:y=0:"
                              f"enable='between(t,{start:.3f},{end:.3f})':eof_action=pass{tnxt}")
                cur = tnxt
                idx += 1
                snd = _sound_path(layer.get("sound"))
                if snd:
                    cmd += ["-t", f"{dur:.3f}", "-i", snd]
                    svol = _num(layer.get("soundVolume"), 0, 4, 1.0)
                    audio_parts.append(f"[{idx}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,"
                                       f"loudnorm=I=-16:TP=-1.5:LRA=11,"   # same level-matching as the other sound path
                                       f"adelay={int(start*1000)}|{int(start*1000)},volume={svol:.2f}[s{n}]")
                    idx += 1
                continue

            path = sources.get(str(layer.get("src") or ""))
            if not path or not os.path.exists(path):
                logger.warning("[meme] layer %s has no resolved source — skipped", n)
                continue

            if kind == "audio":
                # A music bed (mp3/m4a/ogg/wav). No video chain at all — it contributes nothing to the
                # composite, only a label to the amix below. That is why this branch never touches `cur`.
                if not _has_audio(path):
                    logger.warning("[meme] audio layer %s carries no audio stream — skipped", n)
                    continue
                # TRUNCATE at the end of the timeline. The song does not extend the project (see `ends`
                # above), so a clip that starts inside the timeline is cut where the video ends, and one
                # starting past the end is dropped entirely rather than fed to ffmpeg as a zero-length input.
                a_start = min(start, duration)
                a_dur = min(dur, duration - a_start)
                if a_dur <= 0.05:
                    continue
                vol = _num(layer.get("volume"), 0, 4, 1.0)
                a_trim = _num(layer.get("trim"), 0, MAX_DURATION, 0)   # skip into the song
                cmd += ["-ss", f"{a_trim:.3f}", "-t", f"{a_dur:.3f}", "-i", path]
                # Resample/relayout explicitly: an arbitrary user mp3 can be 44.1 kHz mono while the mix's
                # silent base is 48 kHz stereo. ffmpeg would auto-insert the conversion, but doing it here
                # keeps adelay's per-channel argument list honest (it is written for stereo).
                aparts = [f"atrim=0:{a_dur:.3f}", "asetpts=PTS-STARTPTS",
                          "aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
                if layer.get("fade"):
                    # Truncation makes a hard cut mid-song, so an out-ramp is the default for music
                    # (unlike the sound-effect catalogue, which is meant to hit sharply).
                    f = min(1.0, max(0.2, a_dur / 8))
                    aparts.append(f"afade=t=in:st=0:d={f:.2f}")
                    aparts.append(f"afade=t=out:st={max(0.0, a_dur - f):.2f}:d={f:.2f}")
                aparts.append(f"adelay={int(a_start*1000)}|{int(a_start*1000)}")
                aparts.append(f"volume={vol:.2f}")
                audio_parts.append(f"[{idx}:a]" + ",".join(aparts) + f"[m{n}]")
                idx += 1
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
                # A VP9-alpha .webm layer (e.g. an effect overlay) MUST be decoded with libvpx-vp9 —
                # ffmpeg's native `vp9` decoder silently ignores the alpha layer, so the overlay would
                # composite fully opaque (a black box over everything beneath it). Decoder is an INPUT
                # option, so it goes before -i.
                _dec = ["-c:v", "libvpx-vp9"] if str(path).lower().endswith(".webm") else []
                cmd += _dec + ["-ss", f"{trim:.3f}", "-t", f"{dur:.3f}", "-i", path]

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

            if kind == "video" and layer.get("mute") is not True and _has_audio(path):
                vol = _num(layer.get("volume"), 0, 4, 1.0)
                audio_parts.append(f"[{idx}:a]adelay={int(start*1000)}|{int(start*1000)},"
                                   f"volume={vol:.2f}[a{n}]")
            idx += 1

            # Per-layer SOUND effect (the AI-chat catalogue: curb, fahh, sopranos…). Added as its own input,
            # trimmed to the layer's window and delayed to its start, then mixed with everything else — so a
            # different sound can land on each layer instead of one soundtrack for the whole meme.
            snd = _sound_path(layer.get("sound"))
            if snd:
                cmd += ["-t", f"{dur:.3f}", "-i", snd]
                svol = _num(layer.get("soundVolume"), 0, 4, 1.0)
                audio_parts.append(f"[{idx}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,"
                                   # The sound catalogue was recorded at wildly different levels (cheers and
                                   # gong are far quieter than the rest), so a raw mix is inconsistent.
                                   # loudnorm brings every clip to the same perceived loudness before the
                                   # per-layer volume is applied, so "add a sound" sounds the same each time.
                                   f"loudnorm=I=-16:TP=-1.5:LRA=11,"
                                   f"adelay={int(start*1000)}|{int(start*1000)},volume={svol:.2f}[s{n}]")
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
            # A SILENT BASE track for the whole project, mixed in alongside the real audio — the audio
            # counterpart of the colour base the video is composited onto, and the fix for "the meme has
            # no sound at all". Every sound is `loudnorm,adelay=<start>`, and an amix whose inputs ALL
            # begin after t=0 terminates immediately: the output came out as ~0.01s of silence and the
            # render still succeeded, so a meme whose only sound sat on a layer that wasn't first (media
            # layers append to the end of the timeline, so the second clip you add always starts late)
            # rendered mute. One input that runs from 0 to the end keeps the mix alive for the whole
            # project. It also makes a sound's loudness independent of when it starts.
            cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            # Each entry ends in its own output label ("…volume=1.00[a3]"); mix exactly those, by name.
            # Layer indexes are not contiguous (text and skipped layers leave gaps), so the labels have
            # to come from the chains themselves rather than from a range().
            labels = [p.rsplit("[", 1)[-1].rstrip("]") for p in audio_parts]
            mix_in = f"[{idx}:a]" + "".join(f"[{l}]" for l in labels)
            idx += 1
            filtergraph += ";" + ";".join(audio_parts)
            filtergraph += f";{mix_in}amix=inputs={len(audio_parts) + 1}:dropout_transition=0:normalize=0[aout]"
            # -ar 48000 is not cosmetic: loudnorm runs its dynamic mode at 192 kHz, and left alone the AAC
            # encoder takes the highest rate it supports (96 kHz) — which plenty of players and phone
            # hardware decoders won't touch, giving a file that looks fine in ffprobe and plays silent.
            maps += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
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
