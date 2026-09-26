"""Turn a clip of somebody's voice into the reference the voice model wants.

ONE implementation, shared by the `voice` and `talk` commands (command_service/gen.py) and the talking
reply bot (talkbot_service) -- so they cannot drift on the format, the length cap or how a bad clip is
worded.
"""
import asyncio
import os


async def normalize_reference(data: bytes, filename: str, tmp_dir: str):
    """Returns ``(wav_path, wav_bytes, error)`` -- mono 24 kHz WAV trimmed to `voice_max_ref_seconds`,
    or ``(None, None, sentence)`` when no audio can be read out of the clip. A VIDEO is fine: ffmpeg
    pulls the audio track out."""
    from app.services import media_service, settings_store

    # The upload keeps its own filename, so it goes in a SUBDIRECTORY: written beside the output, an
    # attachment that happens to be called `ref.wav` IS the output path, and ffmpeg refuses ("cannot
    # edit existing files in-place") on a clip that is otherwise perfect.
    in_dir = os.path.join(tmp_dir, "in")
    os.makedirs(in_dir, exist_ok=True)
    src = os.path.join(in_dir, os.path.basename(filename or "ref") or "ref")
    with open(src, "wb") as f:
        f.write(data)
    # Normalise to what the model wants: mono 24kHz WAV, trimmed to the reference cap. A long
    # reference buys nothing (the model uses a few seconds) and costs upload + memory on every
    # forwarded request, so the cap is enforced HERE, once, before any of that.
    max_ref = int(float(settings_store.get("voice_max_ref_seconds", "30") or 30))
    wav_path = os.path.join(tmp_dir, "ref.wav")
    ff = media_service.resolve_ffmpeg()
    proc = await asyncio.create_subprocess_exec(
        ff, "-y", "-i", src, "-t", str(max_ref), "-ar", "24000", "-ac", "1", wav_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(wav_path):
        return None, None, ("Couldn't read any audio out of that clip: "
                            + err[-200:].decode("utf-8", "replace"))
    with open(wav_path, "rb") as f:
        return wav_path, f.read(), None
