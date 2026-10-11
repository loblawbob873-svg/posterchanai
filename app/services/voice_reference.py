"""Turn a clip of somebody's voice into the reference the voice model wants.

ONE implementation, shared by the `voice` and `talk` commands (command_service/gen.py), the talking
reply bot (talkbot_service), the studio's /client/voice/speak + /client/voice/reference and the node API
(/api/generate-voice) -- so they cannot drift on the format, the length cap, the SIZE cap or how a bad
clip is worded.

Two caps, and they are different questions:
  * LENGTH -- `voice_max_ref_seconds` (30 s). Enforced by TRIMMING, never by refusing: the model reads a
    few seconds, so a long clip is cut, not rejected.
  * SIZE -- MAX_REFERENCE_BYTES, the most we will accept on the wire. It has to sit far above a phone
    VIDEO, which is what a saved voice very often is (the studio accepts video/*): a 16 MB cap refused a
    perfectly usable sample with "that reference clip is too long" -- a sentence about LENGTH for a
    check about BYTES, reported against a one-word line it had nothing to do with. A refusal names the
    size, in megabytes (`too_large`).
"""
import asyncio
import os

# The most a reference upload may weigh. Uploads are spooled to disk in chunks (`spool_upload`), so
# this bounds disk + ffmpeg work, not memory.
MAX_REFERENCE_BYTES = 200 * 1024 * 1024

_CHUNK = 1024 * 1024


class ReferenceTooLarge(ValueError):
    pass


def too_large(limit: int | None = None) -> str:
    """The one refusal for an oversized reference: what it is about (size), and the number."""
    mb = int((MAX_REFERENCE_BYTES if limit is None else limit) // (1024 * 1024))
    return (f"that file is over {mb} MB — a voice only needs a few seconds of speech, "
            "so trim it or pick a smaller clip")


async def spool_upload(upload, dest_path: str, limit: int | None = None) -> int:
    """Copy a Starlette UploadFile to `dest_path` in chunks; returns the byte count.

    Raises ReferenceTooLarge past `limit` (default MAX_REFERENCE_BYTES), having written no more than
    that -- the whole upload is never held in memory."""
    limit = MAX_REFERENCE_BYTES if limit is None else limit
    total = 0
    with open(dest_path, "wb") as out:
        while True:
            chunk = await upload.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ReferenceTooLarge(too_large(limit))
            out.write(chunk)
    return total


def max_ref_seconds() -> int:
    from app.services import settings_store
    return int(float(settings_store.get("voice_max_ref_seconds", "30") or 30))


def wav_seconds(wav: bytes) -> float:
    """Duration of the mono 16-bit 24 kHz WAV this module writes (header-agnostic estimate)."""
    return max(0, len(wav or b"") - 44) / (24000 * 2)


async def normalize_reference_file(src: str, tmp_dir: str):
    """`normalize_reference` for a clip already on disk (an upload spooled by `spool_upload`)."""
    from app.services import media_service

    # Normalise to what the model wants: mono 24kHz WAV, trimmed to the reference cap. A long
    # reference buys nothing (the model uses a few seconds) and costs upload + memory on every
    # forwarded request, so the cap is enforced HERE, once, before any of that.
    os.makedirs(tmp_dir, exist_ok=True)
    wav_path = os.path.join(tmp_dir, "ref.wav")
    ff = media_service.resolve_ffmpeg()
    proc = await asyncio.create_subprocess_exec(
        ff, "-y", "-i", src, "-t", str(max_ref_seconds()), "-ar", "24000", "-ac", "1", wav_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(wav_path):
        return None, None, ("Couldn't read any audio out of that clip: "
                            + (err or b"")[-200:].decode("utf-8", "replace"))
    with open(wav_path, "rb") as f:
        return wav_path, f.read(), None


async def normalize_reference(data: bytes, filename: str, tmp_dir: str):
    """Returns ``(wav_path, wav_bytes, error)`` -- mono 24 kHz WAV trimmed to `voice_max_ref_seconds`,
    or ``(None, None, sentence)`` when the clip is over the size cap or no audio can be read out of
    it. A VIDEO is fine: ffmpeg pulls the audio track out."""
    if len(data or b"") > MAX_REFERENCE_BYTES:
        msg = too_large()
        return None, None, msg[0].upper() + msg[1:]
    # The upload keeps its own filename, so it goes in a SUBDIRECTORY: written beside the output, an
    # attachment that happens to be called `ref.wav` IS the output path, and ffmpeg refuses ("cannot
    # edit existing files in-place") on a clip that is otherwise perfect.
    in_dir = os.path.join(tmp_dir, "in")
    os.makedirs(in_dir, exist_ok=True)
    src = os.path.join(in_dir, os.path.basename(filename or "ref") or "ref")
    with open(src, "wb") as f:
        f.write(data)
    return await normalize_reference_file(src, tmp_dir)
