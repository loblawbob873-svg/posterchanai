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
import math
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
# VAAPI quality. NOT the same scale as x264's -crf: measured on a real 20s render, qp 20 produced 3.14 MB
# against libx264 -crf 20's 1.31 MB — 2.4x, and a meme is uploaded once then downloaded by everyone who
# sees it. qp 26 lands at 1.35 MB, within 2.5% of the CPU encoder's size, so the GPU path costs no extra
# bandwidth. Tuned by measuring (20/24/26/28) rather than assuming the scales match.
_VAAPI_QP = 26
DEFAULT_W, DEFAULT_H, DEFAULT_FPS = 720, 1280, 30
# GIF bounds. Paletted frames do not compress like h264 — size is roughly linear in duration x pixels, so
# these are what keep "export a GIF" from producing a file nobody can upload. 12 fps and a 480px long edge
# is the usual reaction-GIF trade-off; the duration is a hard refusal (see render).
MAX_GIF_DURATION = 20.0
GIF_FPS = 12
GIF_MAX_EDGE = 480

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
        # c=black@0, NOT c=none. `none` does not mean "transparent" — it means "print no background at
        # all", so the corners the rotated frame does not cover keep whatever was already in the output
        # buffer. On a spin that is the previous frame's pixels smeared into all four corners: measured
        # against the same project, the export had green in corners the preview (correctly) left empty,
        # by up to 77px. Naming a fully-transparent colour paints them, which is what was always meant.
        return f"rotate='0.6*t':c=black@0:ow={w}:oh={h}"
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


def _fit_chain(fit: Optional[str], lw: int, lh: int, fps: int) -> list:
    """The scale/pad (or scale/crop) that seats a source inside the layer's lw x lh box.

    "contain" (default) letterboxes the source inside the box; "cover" scales UP until the box is filled
    and crops the overflow — what you actually want from a "fill the canvas" background, where a
    letterboxed image with transparent bars is not filling anything.

    Shared by the layer and by its ERASE MASK, and the sharing is the whole point. The mask is painted in
    the SOURCE's own coordinate space (so it survives resizing, re-fitting or rotating the layer
    afterwards), which means it only covers the pixels it is meant to erase if it is seated into the box
    by EXACTLY this geometry. Two copies of these five filters would drift apart the first time either
    was touched, and the symptom — an erase that is offset or scaled slightly — looks like a brush bug
    rather than a geometry one.
    """
    if str(fit or "").lower() == "cover":
        return [f"scale={lw}:{lh}:force_original_aspect_ratio=increase",
                f"crop={lw}:{lh}",
                "setsar=1", f"fps={fps}", "format=rgba"]
    return [f"scale={lw}:{lh}:force_original_aspect_ratio=decrease",
            f"pad={lw}:{lh}:(ow-iw)/2:(oh-ih)/2:color=black@0",
            "setsar=1", f"fps={fps}", "format=rgba"]


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
    # 1.18 is ALSO written into `.mb-text{line-height}` in client.css — the stage has to step its lines
    # by the same pitch or a wrapped caption sits progressively higher on the stage than in the export
    # (it did: 0.18em per line, cumulative, so only multi-line captions showed it). Change one and you
    # must change the other; scripts/check_meme_render_match.py is what catches it if you don't.
    y_off = int(_num(layer.get("_line_dy"), 0, 200, 0)) * int(round(size * 1.18))
    y_expr = f"{y + y_off}"
    # Optional background box — the "caption bar" look (black text on a solid strip) that a plain outlined
    # caption cannot do. drawtext draws it around THIS drawtext's own text, so with one filter per line the
    # box hugs each line, which is what the client's per-line preview spans mirror. boxborderw scales with
    # the font so the padding looks the same at any size.
    box = ""
    if layer.get("box"):
        box = (f"box=1:boxcolor={_ff_colour(layer.get('boxColor'), 'black')}@"
               f"{_num(layer.get('boxAlpha'), 0, 1, 0.55):.2f}:boxborderw={max(4, size // 5)}:")
    # Drop shadow, offset by a fraction of the font size like the outline width is.
    shadow = ""
    if layer.get("shadow"):
        d = max(2, size // 18)
        shadow = f"shadowcolor=black@0.65:shadowx={d}:shadowy={d}:"
    return (f"drawtext={fontfile}textfile='{{TEXTFILE}}':fontsize={size}:fontcolor={colour}:"
            f"borderw={max(2, size//14)}:bordercolor={stroke}:{box}{shadow}x={x_expr}:y={y_expr}{alpha}:"
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
    # No leading emoji on these two: alpha_effect_catalog() prefixes every entry here with 🧍, so one
    # of its own renders as "🧍 🧍 Carl". (The older entries below double up with a DIFFERENT emoji,
    # which at least reads as decoration rather than a bug.)
    ("carl", "Carl"), ("soyjack", "😮 Soyjaks pointing"), ("lookingaway", "🙈 Looking away (turns to camera)"),
    ("would", "Would (old man)"), ("theraped", "Pointing (anime)"),
    ("jerry", "🎤 Jerry (stand-up)"), ("nothingeverhappens", "🏫 Nothing ever happens"),
    ("nodontthinkiwill", "🙅 No, I don't think I will"), ("ruckus", "Uncle Ruckus"),
]


# Ready-made TRANSPARENT overlay clips shipped in assets/ (`<effect>_<something>.mov`, ProRes 4444).
# They are the effect's own animation on a clear background — exactly what a layer is — but the
# catalogue below only ever listed the hand-drawn character poses, so `beavis`, `reze`, `makima`,
# `rebecca`, `uwu`, `vibe` and `clay` could not be added as layers even though their art was sitting
# right there. Nice labels only; membership is DISCOVERED from the files (see _alpha_clips).
_ALPHA_CLIP_LABELS = {
    "beavis": "🤤 Beavis (laughing)", "clay": "🗿 Clay", "makima": "🔫 Makima (shooting)",
    "rebecca": "💃 Rebecca (dancing)", "reze": "💣 Reze (dancing)", "uwu": "💗 UwU (dancing)",
    "vibe": "🕺 Vibe (dancing)", "gura": "🦈 Gura (shark pog)",
}

_alpha_clip_cache: dict = None


def _alpha_clips() -> dict:
    """{effect_name: {"path", "dur"}} for the transparent overlay clips installed on THIS node.

    Discovered, not hard-coded — the same rule alpha_effect_catalog() states for itself. A clip
    qualifies only if ffprobe reports a real ALPHA pixel format (an opaque .mov would composite as
    an ugly rectangle, the same failure the end-card logo had) and its name maps to an effect the
    app actually has. The effect name is the leading token of the filename: beavis_laugh.mov ->
    beavis. Probed ONCE and cached: this runs on every catalogue fetch, and it is 7 subprocesses."""
    global _alpha_clip_cache
    if _alpha_clip_cache is not None:
        return _alpha_clip_cache
    import glob
    import subprocess as _sp
    from app.services.command_service import CommandService as _C
    from app.services.effects_service._common import _REPO_ROOT
    real_effects = set(_C.MOTION_EFFECTS) | set(_C.ANIMATED_EFFECTS)
    found = {}
    for base in (os.path.join(_REPO_ROOT, "assets"),):
        for p in sorted(glob.glob(os.path.join(base, "*.mov"))):
            name = os.path.basename(p).rsplit(".", 1)[0].split("_")[0].lower()
            if name in found or name not in real_effects:
                continue
            try:
                r = _sp.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=pix_fmt,duration", "-of", "default=nw=1:nk=1", p],
                            capture_output=True, text=True, timeout=20)
                parts = (r.stdout or "").split()
                pix = parts[0] if parts else ""
                if not (pix.startswith("yuva") or pix in ("rgba", "argb", "bgra")):
                    continue        # opaque → not a layer
                dur = float(parts[1]) if len(parts) > 1 else 0.0
            except Exception:
                continue
            found[name] = {"path": p, "dur": round(dur, 2) if dur > 0 else 4.0}
    _alpha_clip_cache = found
    return found


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
    out.append({"name": "nakedman", "label": "🍆 Naked man (dancing)", "pose": False,
                "audio": bool(_nm._nakedman_audio_path()), "dur": 8.0})
    # shrug needs its pose art; audio is a bundled asset.
    if _ch._character_path("shrug"):
        out.append({"name": "shrug", "label": "🤷 Shrug", "pose": True,
                    "audio": bool(_ch._shrug_audio_path()), "dur": 2.7})
    for key, label in _ALPHA_CHARACTERS:
        if _ch._character_path(key):
            # A still pose holds for as long as you like; the two-panel turn has its own beat.
            # `pose` marks a STILL character — one drawing held on screen, as opposed to an
            # animation. Only a pose can be made to TALK: the lip-sync animates a single picture,
            # so running it on a clip that already moves would freeze the movement. `lookingaway`
            # is the two-panel turn, which is an animation however still each panel is.
            out.append({"name": key, "label": f"🧍 {label}", "audio": False,
                        "pose": key != "lookingaway",
                        "dur": _ch.LOOKINGAWAY_ALPHA_DUR if key == "lookingaway" else 6.0})
    # The shipped transparent clips (beavis, reze, makima, …). `dur` is the clip's REAL length, read
    # off the file, so the timeline slot fits the animation instead of a guessed 6s. Each has its
    # sound as a separate mp3 — the clip itself stays silent, exactly like every other alpha layer.
    for name, meta in sorted(_alpha_clips().items()):
        out.append({"name": name, "label": _ALPHA_CLIP_LABELS.get(name, f"🎬 {name.title()}"),
                    "pose": False,      # a shipped clip already animates
                    "audio": bool(_sound_path(name)), "dur": meta["dur"]})
    return out


# Names a caller may still be holding for an effect the catalogue now lists under its real name. A
# client caches the catalogue, and `anyways` is what people have typed for months, so both have to
# keep resolving — mirrors CommandService.COMMAND_ALIASES for the command path.
_ALPHA_ALIASES = {"anyways": "lookingaway", "lookaway": "lookingaway"}

_alpha_alias_cache: dict = None


def _alpha_aliases() -> dict:
    """alias -> the catalogue name that draws the SAME artwork.

    Every character pose has nicknames (`seinfeldjerry` for jerry, `brutananadilewski` for carl,
    `soyjak`, `oldman`, `rabbi`, `unclerukus`, …) and they live in ONE place: `_CHARACTERS` in
    effects_service/_common.py, which maps each of them to a PNG. The catalogue, though, is keyed by
    the canonical name — so `canonical_alpha_effect` used to hand an alias straight back and every
    check that then compared it against the catalogue failed on a name whose artwork was sitting
    right there. `render_alpha_effect` raised "unknown effect: seinfeldjerry", and `_pose_art_path`
    returned "" — which is exactly "this character cannot be made to talk", for a character that can.
    This is the alias-before-the-allowlist rule CLAUDE.md states for effects, applied to poses.

    DERIVED from the artwork rather than hand-listed, so a nickname added to `_CHARACTERS` cannot
    fall out of step with this. The explicit `_ALPHA_ALIASES` entries stay on top: `anyways` and
    `lookingaway` are DIFFERENT files (the one-panel still vs the two-panel turn), so no file
    comparison can relate them."""
    global _alpha_alias_cache
    if _alpha_alias_cache is not None:
        return _alpha_alias_cache
    out = {}
    try:
        from app.services.effects_service import _common as _c
        art = getattr(_c, "_CHARACTERS", {}) or {}
        canon = {}
        for key, _label in list(_ALPHA_CHARACTERS) + [("shrug", "")]:
            f = art.get(key)
            if f:
                canon.setdefault(f, key)
        for alias, f in art.items():
            target = canon.get(f)
            if target and target != alias:
                out[alias] = target
    except Exception:
        out = {}
    out.update(_ALPHA_ALIASES)   # the hand-written ones win — they relate different files on purpose
    _alpha_alias_cache = out
    return out


def canonical_alpha_effect(name: str) -> str:
    """An effect name resolved to the one the catalogue actually lists."""
    n = (name or "").strip().lower()
    return _alpha_aliases().get(n, n)


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
    clips = _alpha_clips()
    if name in clips:
        # Already a transparent clip on disk — only the codec differs, so there is nothing to draw.
        data = media_service.alpha_clip_to_video(clips[name]["path"], dur=dur)
    elif name == "nakedman":
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


def _source_duration(path: str) -> float:
    """Length of a video source in seconds, or 0.0 when it can't be determined.

    Used only to decide whether a layer's slot outruns its own footage; a 0.0 (unprobeable) answer means
    "assume it fits" and changes nothing, so a probe failure can never break a render that worked before.
    """
    try:
        p = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                            "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=20)
        return max(0.0, float((p.stdout or "").strip().splitlines()[0]))
    except Exception:
        return 0.0


def _mp4_to_gif(mp4: bytes, w: int, h: int) -> bytes:
    """MP4 bytes -> looping GIF bytes, via the two-pass palette (palettegen to a PNG, then paletteuse
    against it).

    NEVER `palettegen=stats_mode=diff` here. That option weights pixels that CHANGE between frames, and on
    a meme — a cut between two mostly-flat shots — it dropped whole colours from the palette outright:
    measured on a red-then-blue two-clip project, the generated palette held two reds and NO BLUE, so
    paletteuse mapped every frame of the second clip to red. The result was a GIF with the correct
    container, the correct frame count and the correct duration in which nothing ever changed — which is
    why test_gif_actually_animates compares pixels at both ends rather than trusting any of those three.
    The default (full) stats mode is correct and costs one pass over an already-capped clip.

    Two passes rather than the one-pass split/palettegen/paletteuse form because it reuses the PROVEN video
    path byte-for-byte: whatever the encoder ladder produced is what gets converted, so a GIF can never
    disagree with the MP4 of the same project.

    The long edge is capped at GIF_MAX_EDGE and the rate at GIF_FPS — a GIF is paletted frames with no
    inter-frame compression worth the name, so those two caps are the difference between a file that can
    be posted and one that cannot."""
    ffmpeg = media_service.resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available on this node")
    # Cap the LONG edge, whichever it is, and let the other side follow (-2 keeps it even).
    scale = (f"scale={min(w, GIF_MAX_EDGE)}:-2" if w >= h else f"scale=-2:{min(h, GIF_MAX_EDGE)}")
    pre = f"fps={GIF_FPS},{scale}:flags=lanczos"
    tmp = tempfile.mkdtemp(prefix="pcmemegif-")
    try:
        src = os.path.join(tmp, "in.mp4")
        pal = os.path.join(tmp, "pal.png")
        out = os.path.join(tmp, "out.gif")
        with open(src, "wb") as fh:
            fh.write(mp4)
        p1 = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", src,
                             "-vf", f"{pre},palettegen", pal],
                            capture_output=True, timeout=_RENDER_TIMEOUT_S)
        if p1.returncode != 0 or not os.path.exists(pal):
            raise RuntimeError("gif palette failed: " + (p1.stderr or b"").decode("utf-8", "replace")[-300:])
        p2 = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", src, "-i", pal,
                             "-filter_complex", f"[0:v]{pre}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
                             "-loop", "0", "-an", out],
                            capture_output=True, timeout=_RENDER_TIMEOUT_S)
        if p2.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError("gif encode failed: " + (p2.stderr or b"").decode("utf-8", "replace")[-300:])
        with open(out, "rb") as fh:
            data = fh.read()
        logger.info("[meme] gif %s (from %s of mp4)",
                    media_service._human_size(len(data)), media_service._human_size(len(mp4)))
        return data
    finally:
        try:
            for f in os.listdir(tmp):
                os.unlink(os.path.join(tmp, f))
            os.rmdir(tmp)
        except Exception:
            pass


def render(edit: dict, sources: dict) -> tuple:
    """Render the edit list. `sources` maps a layer's `src` key -> local file path (the caller resolves
    and fetches URLs/Blossom hashes, so this stays a pure renderer with no network of its own).

    Returns (bytes, content_type). `edit["fmt"]` picks the container:
      "mp4"  (default) — the branded short clip, encoded on the shared encoder ladder
      "gif"  — a looping GIF, for the places that still only take one (and for a reaction image)
      "png"  — ONE frame, at `edit["still"]` seconds: a meme is very often a picture, and exporting a
               still used to mean rendering the video and screenshotting it
    GIF and PNG carry no audio at all, so a sound-only difference is invisible in them by design.

    Raises ValueError on an edit list we refuse, RuntimeError if ffmpeg fails.
    """
    ffmpeg = media_service.resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available on this node")

    fmt = str(edit.get("fmt") or "mp4").strip().lower()
    if fmt not in ("mp4", "gif", "png"):
        raise ValueError(f"unknown export format: {fmt}")
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
    # A GIF is uncompressed-ish paletted frames, so its size grows linearly and fast: a 60s project at
    # 480px/12fps is tens of megabytes and would very likely hit _RENDER_TIMEOUT_S having produced
    # something nobody can post. Refuse with the fix rather than silently truncating the meme.
    if fmt == "gif" and duration > MAX_GIF_DURATION:
        raise ValueError(f"a GIF has to be short — this project is {duration:.0f}s and the limit is "
                         f"{MAX_GIF_DURATION:.0f}s. Trim it, or export MP4.")

    # A GIF is the MP4 render, converted — see _mp4_to_gif for why, and for the palette option that must
    # never come back. Capped at MAX_GIF_DURATION above, so the extra pass is cheap.
    if fmt == "gif":
        mp4, _ = render({**edit, "fmt": "mp4"}, sources)
        return _mp4_to_gif(mp4, w, h), "image/gif"

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
            # Playback speed for a VIDEO layer. 1 = untouched, 2 = twice as fast, 0.5 = half speed. `dur` is
            # always the length of the layer's SLOT on the timeline, so a faster clip has to be fed MORE
            # source to fill it — hence src_dur below — and then compressed back with setpts. Getting that
            # backwards gives a clip that ends early and freezes on its last frame for the rest of the slot.
            speed = _num(layer.get("speed"), 0.25, 4.0, 1.0)
            if kind != "video" or abs(speed - 1.0) < 0.01:
                speed = 1.0
            src_dur = min(MAX_DURATION, dur * speed)

            if kind == "image":
                # An AVIF/HEIC layer is ISOBMFF, whose demuxer has no `loop` either — same abort as
                # the GIF case below, one screenshot-from-a-browser away. Converted to a PNG up front
                # (written beside the source, so the caller's tmpdir sweep removes it), so `-loop 1`
                # below is always legal.
                path = media_service.loopable_still(path)
                if str(path).lower().endswith(".gif"):
                    # A GIF is demuxed by the GIF demuxer, which has NO `loop` option — `-loop`
                    # belongs to image2. Passing it did not degrade to a still: ffmpeg aborted with
                    # "Option loop not found" and the WHOLE render failed, so one GIF layer broke the
                    # entire meme. `-ignore_loop 0` is the GIF spelling, and it also makes an ANIMATED
                    # gif animate and repeat for the length of its slot instead of freezing on frame 1.
                    cmd += ["-ignore_loop", "0", "-t", f"{dur:.3f}", "-i", path]
                else:
                    cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-i", path]
            else:
                # A VP9-alpha .webm layer (e.g. an effect overlay) MUST be decoded with libvpx-vp9 —
                # ffmpeg's native `vp9` decoder silently ignores the alpha layer, so the overlay would
                # composite fully opaque (a black box over everything beneath it). Decoder is an INPUT
                # option, so it goes before -i.
                _dec = ["-c:v", "libvpx-vp9"] if str(path).lower().endswith(".webm") else []
                cmd += _dec + ["-ss", f"{trim:.3f}", "-t", f"{src_dur:.3f}", "-i", path]

            # ERASE MASK (the builder's ✂ tool): a PNG the size of the layer's SOURCE whose alpha is the
            # part to KEEP — opaque where the picture stays, transparent where the user rubbed it out.
            # Resolved through `sources` like any other layer media, so it is fetched by the same guarded
            # path and this stays a renderer with no network of its own. Added as its own input right
            # after the layer's, which is why `idx` advances by two below.
            mask_path = sources.get(str(layer.get("mask") or "")) if layer.get("mask") else None
            mask_idx = None
            if mask_path and os.path.exists(mask_path):
                mask_idx = idx + 1
                cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-i", mask_path]

            chain = _fit_chain(layer.get("fit"), lw, lh, fps)
            # Speed goes FIRST, before `fps` resamples: restretching the timestamps after the frame rate has
            # been fixed leaves a stream running at fps*speed, which every downstream filter built for `fps`
            # (zoompan's d=frames, the fade ramps) then measures wrongly. Ahead of it, `fps` does the
            # frame-dropping/duplicating and everything after sees a normal stream at the project rate.
            if speed != 1.0:
                chain.insert(0, f"setpts=PTS/{speed:.4f}")
            # A slot LONGER than the footage left after the in-point: hold the last frame for the rest of it.
            # Without this the layer's stream simply ends, overlay's eof_action=pass lets the composite show
            # through, and the tail of the slot is background — while the EDITOR's preview holds the last
            # frame, because an HTML video element clamps to its duration. The two disagreeing is what made a
            # deliberately-long slot look broken, and it is why the builder used to silently shorten `dur`
            # back to the source length instead (see bindTrim's ready() in meme.js). Padding here is what
            # makes the length the user typed the length they get.
            if kind == "video":
                _sdur = _source_duration(path)
                if _sdur and trim + src_dur > _sdur + 0.05:
                    chain.append(f"tpad=stop_mode=clone:stop_duration={dur:.3f}")
            # APPLY THE ERASE MASK here — after the source is seated in its box, before flip/effect/rotate.
            # That order is what the editor paints against: the eraser shows the layer's own artwork
            # untransformed, so the stroke means "this part of the picture", not "this part of the screen".
            # Masking after a rotate would erase a fixed region of the frame while the picture turned
            # underneath it.
            #
            # It MULTIPLIES the existing alpha instead of replacing it (which is all `alphamerge` alone
            # would do). The stream already carries meaningful transparency by this point — a cut-out PNG,
            # a VP9-alpha effect layer, and the transparent letterbox bars `pad` just added — and replacing
            # the alpha with the mask makes every one of those opaque: the bars come back as black,
            # and erasing part of a cut-out un-cuts the rest of it.
            if mask_idx is not None:
                head = f"[{idx}:v]"
                chains.append(head + ",".join(chain) + f"[mp{n}]")
                # Same _fit_chain as the layer — see its docstring. alphaextract turns the mask's own alpha
                # into the gray plane blend/alphamerge work on.
                chains.append(f"[{mask_idx}:v]" + ",".join(_fit_chain(layer.get("fit"), lw, lh, fps))
                              + f",alphaextract[mk{n}]")
                chains.append(f"[mp{n}]split[mA{n}][mB{n}]")
                chains.append(f"[mB{n}]alphaextract[mBa{n}]")
                # repeatlast: the mask is one still frame looped, the layer may be a video — hold the mask
                # rather than letting framesync end the blend at the shorter input.
                chains.append(f"[mBa{n}][mk{n}]blend=all_mode=multiply:repeatlast=1[mNa{n}]")
                chains.append(f"[mA{n}][mNa{n}]alphamerge[mM{n}]")
                chain = []
            # MIRROR before the effect (hflip/vflip preserve the frame size, so the effect chain still
            # sees the lw x lh box it was built for) and ROTATE after it — so a spin/zoom animates the
            # upright artwork and the whole result is then turned, rather than the effect's own geometry
            # being rotated out from under it.
            if layer.get("flipH"):
                chain.append("hflip")
            if layer.get("flipV"):
                chain.append("vflip")
            fx = _fx_chain(effect, lw, lh, dur, fps)
            if fx:
                chain.append(fx)
            # TRANSITION ramps — the crossfade. These are alpha fades on the layer's OWN stream, in its own
            # local time (the shift onto the project timeline happens further down), so a clip whose slot
            # overlaps the previous one fades up while that one fades out: a real dissolve, with no xfade
            # filter and no change to the one-pass overlay composite. Kept separate from the `fade` EFFECT
            # because `effect` is a single choice — a clip should be able to dissolve AND be black-and-white.
            xin = _num(layer.get("xin"), 0, 5, 0)
            xout = _num(layer.get("xout"), 0, 5, 0)
            if xin > 0.01:
                chain.append(f"fade=t=in:st=0:d={min(xin, dur):.2f}:alpha=1")
            if xout > 0.01:
                _d = min(xout, dur)
                chain.append(f"fade=t=out:st={max(0.0, dur - _d):.2f}:d={_d:.2f}:alpha=1")
            # Free rotation about the layer's CENTRE. `ow=rotw(a)/oh=roth(a)` grows the frame to hold the
            # whole rotated image so the corners aren't sliced off; that growth is symmetric, so the
            # overlay origin has to move back by half of it or the layer would visibly drift down-right as
            # you rotate. fillcolor=none keeps the new corners transparent (the chain is already rgba).
            ox, oy = lx, ly
            rot = _num(layer.get("rotate"), -360, 360, 0)
            if abs(rot) > 0.01:
                rad = math.radians(rot)
                c, sn = abs(math.cos(rad)), abs(math.sin(rad))
                ow, oh = lw * c + lh * sn, lw * sn + lh * c        # same as rotw()/roth()
                # fillcolor=black@0, not `none` — see the spin effect in _fx_chain for what `none`
                # actually does. The grown ow/oh happen to give this a fresh buffer most of the time,
                # which is why it has not misbehaved here, but "most of the time" is not a guarantee
                # and the two rotates may as well state the same, defined thing.
                chain.append(f"rotate={rad:.6f}:ow=rotw({rad:.6f}):oh=roth({rad:.6f}):fillcolor=black@0")
                ox = lx - int(round((ow - lw) / 2))
                oy = ly - int(round((oh - lh) / 2))
            if opacity < 1.0:
                chain.append(f"colorchannelmixer=aa={opacity:.3f}")
            # setpts shifts the layer to its slot on the project timeline; the overlay `enable` then
            # gates it. Both are needed: without setpts the clip plays from t=0 regardless of `start`.
            chain.append(f"setpts=PTS-STARTPTS+{start:.3f}/TB")
            # A masked layer already emitted its front half above and continues from the mask's output;
            # `chain` is never empty here (the setpts shift above always lands in it), so this can't
            # produce a chainless "[a][b]" link.
            chains.append((f"[mM{n}]" if mask_idx is not None else f"[{idx}:v]")
                          + ",".join(chain) + f"[l{n}]")

            nxt = f"[v{n}]"
            chains.append(f"{cur}[l{n}]overlay=x={ox}:y={oy}:"
                          f"enable='between(t,{start:.3f},{end:.3f})':eof_action=pass{nxt}")
            cur = nxt

            if kind == "video" and layer.get("mute") is not True and _has_audio(path):
                vol = _num(layer.get("volume"), 0, 4, 1.0)
                # A sped-up clip's sound has to be sped up with it or it runs past the picture. atempo only
                # accepts 0.5-2.0 per instance, so anything outside that is CHAINED — a single
                # atempo=4 is silently rejected by the filter and the audio stays at 1x against a 4x picture.
                _tempo = []
                _s = speed
                while _s > 2.0 + 1e-6:
                    _tempo.append("atempo=2.0"); _s /= 2.0
                while _s < 0.5 - 1e-6:
                    _tempo.append("atempo=0.5"); _s /= 0.5
                if abs(_s - 1.0) > 0.01:
                    _tempo.append(f"atempo={_s:.4f}")
                audio_parts.append(f"[{idx}:a]" + "".join(t + "," for t in _tempo)
                                   + f"adelay={int(start*1000)}|{int(start*1000)},"
                                   f"volume={vol:.2f}[a{n}]")
            # TWO inputs were added for a masked layer (the source and its mask), so the next layer's
            # input index has to clear both. Advancing by one would silently point every later layer at
            # the wrong input — a mask erasing one layer while another rendered somebody else's footage.
            idx += 2 if mask_idx is not None else 1

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

        # ---- A STILL leaves here: no h264 ladder, no audio ----
        if fmt == "png":
            # ONE frame, taken at the client's playhead. Done with `trim` on the finished composite rather
            # than an output `-ss`: the overlays are gated on ABSOLUTE t, so the frame has to be selected
            # after compositing, and trim is frame-exact where a seek's semantics depend on which side of
            # -i it lands. Clamped inside the project or there is no frame to take.
            at = _num(edit.get("still"), 0, MAX_DURATION, 0)
            at = max(0.0, min(at, max(0.0, duration - 1.0 / fps)))
            chains.append(f"{cur}trim=start={at:.3f}:duration={1.0/fps:.4f},setpts=PTS-STARTPTS,"
                          f"format=rgb24[vout]")
            out = os.path.join(tmp, "out.png")
            full = cmd + ["-filter_complex", ";".join(chains), "-map", "[vout]", "-an",
                          "-frames:v", "1", out]
            try:
                p = subprocess.run(full, capture_output=True, timeout=_RENDER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"render timed out after {_RENDER_TIMEOUT_S}s — try fewer layers")
            if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
                err = (p.stderr or b"").decode("utf-8", "replace")[-500:]
                raise RuntimeError(f"ffmpeg failed: {err}")
            with open(out, "rb") as fh:
                data = fh.read()
            logger.info("[meme] rendered still %dx%d @%.2fs, %d layers -> %s",
                        w, h, at, len(layers), media_service._human_size(len(data)))
            return data, "image/png"

        # Keep the video tail addressable: the GPU encoder needs a DIFFERENT one (see the loop below),
        # and this exact string — label included — is what gets swapped, so there is nothing else in the
        # graph it could match.
        _vtail_sw = f"{cur}format=yuv420p[vout]"
        chains.append(_vtail_sw)
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
            # VAAPI does not encode software frames. Naming the encoder is not enough: it needs its
            # DEVICE and the frames uploaded to a GPU surface (format=nv12,hwupload), exactly like
            # media_service._video_encode_cmd does for the compress path. Without both, ffmpeg aborted
            # with "Terminating thread with return code -22 (Invalid argument)" on EVERY render, so the
            # ladder silently fell through to libx264 and every meme was encoded on the CPU — the GPU
            # branch had never once succeeded. Probed on this box: with these args it encodes fine.
            fg, pre, tail = filtergraph, [], ["-pix_fmt", "yuv420p"]
            if enc == "h264_vaapi":
                fg = filtergraph.replace(_vtail_sw, f"{cur}format=nv12,hwupload[vout]", 1)
                pre = ["-vaapi_device", media_service._render_node()]
                tail = ["-qp", str(_VAAPI_QP)]   # surface format is the GPU's; -pix_fmt would fight it
            full = cmd[:1] + pre + cmd[1:] + ["-filter_complex", fg] + maps + \
                   ["-c:v", enc, "-t", f"{duration:.3f}"] + tail + ["-movflags", "+faststart"]
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
                return data, "video/mp4"
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
