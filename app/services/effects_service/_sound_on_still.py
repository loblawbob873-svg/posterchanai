"""The "a sound over your picture" effects, built from one table row each.

Fifty-odd effects (`hava`, `curb`, `sopranos`, …) were each three hand-copied functions that differed
only in a name, an mp3, a length, a heading and an emoji: find the mp3, turn the first image into an
MP4 of that length with the sound over it, and wrap the result as an attachment with a one-line
summary. `register()` builds the same three functions from a row and puts them in the calling
module, under the same names, so everything that looks them up keeps working unchanged:

  * `_<name>_audio_path()` — the Meme Builder discovers its sound list by scanning the submodules
    for exactly this name (meme_builder_service.sound_names), so it must be a module attribute;
  * `add_<name>(image_data, source_filename)` and `<name>_attachments(attachments)` — public, and
    re-exported by the package's `from .audioN import *`.

An effect that does anything more (a dance overlay, a start offset, every image instead of the first)
keeps its own hand-written functions.
"""
import os
from pathlib import Path

from ._common import _human_size, is_image, logger


def register(ns: dict, name: str, heading: str, lead: str, candidates, duration: float, missing: str):
    """Define `_<name>_audio_path`, `add_<name>` and `<name>_attachments` in the namespace `ns`."""

    def audio_path() -> str:
        """First existing mp3 from the candidate list ("" if none)."""
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return ""

    def add(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
        """Turn a still image into an MP4 with this effect's sound over it. MP4 bytes."""
        from app.services.media_service import image_audio_to_video
        audio = audio_path()
        if not audio:
            raise RuntimeError(missing)
        return image_audio_to_video(image_data, source_filename, audio, duration=duration)

    def attachments(attachments):
        """Turn the first image attachment into this effect's MP4 (several images go in as a list)."""
        images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
        if not images:
            return [], "No image — attach an image first."
        filename, data, _ = images[0]
        data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
        stem = Path(filename).stem or "image"
        try:
            # Looked up in the module, not closed over: a test (or a wrapper) that replaces
            # `add_<name>` there must be what runs, exactly as it was with the hand-written copies.
            result = ns[f"add_{name}"](data, filename)
            out = {"filename": f"{stem}_{name}.mp4", "data": result, "content_type": "video/mp4"}
            return [out], f"## {heading}\n\n{lead} {filename}: {_human_size(len(result))}"
        except Exception as e:
            logger.error(f"{name} failed for {filename}: {e}", exc_info=True)
            return [], f"❌ {filename}: {e}"

    audio_path.__name__ = f"_{name}_audio_path"
    add.__name__ = f"add_{name}"
    attachments.__name__ = f"{name}_attachments"
    for fn in (audio_path, add, attachments):
        fn.__qualname__ = fn.__name__
        fn.__module__ = ns.get("__name__", fn.__module__)
        ns[fn.__name__] = fn
