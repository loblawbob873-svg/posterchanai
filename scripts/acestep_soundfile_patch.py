#!/usr/bin/env python3
"""Patch ACE-Step's audio save to be torchcodec-free (for Intel XPU / AMD ROCm).

`torchaudio.save` routes through torchcodec on torchaudio>=2.9, and torchcodec ships CUDA-only
prebuilt binaries — so on non-NVIDIA GPUs (where we drop torchcodec) the save path fails with
"TorchCodec is required for save_with_torchcodec". ACE-Step's audio DECODE already falls back to
soundfile, but SAVE does not. This replaces the torchaudio.save calls in acestep/audio_utils.py
with soundfile.write. Idempotent; safe no-op if already patched or upstream changed.

Usage: python acestep_soundfile_patch.py /path/to/ACE-Step-1.5
"""
import pathlib
import sys

REPLACEMENTS = [
    # _save_mp3: temp WAV write before the ffmpeg->mp3 step.
    (
        "            torchaudio.save(\n"
        "                str(temp_wav_path),\n"
        "                tensor_to_save,\n"
        "                int(target_sample_rate),\n"
        "                channels_first=True,\n"
        "                backend='soundfile',\n"
        "            )",
        "            # PATCH (PosterChanAI): soundfile instead of torchaudio.save (torchcodec-free).\n"
        "            import soundfile as _sf\n"
        "            _sf.write(str(temp_wav_path), tensor_to_save.transpose(0, 1).cpu().numpy(), int(target_sample_rate))",
    ),
    # FLAC/WAV branch.
    (
        "                torchaudio.save(\n"
        "                    str(output_path),\n"
        "                    audio_tensor,\n"
        "                    sample_rate,\n"
        "                    channels_first=True,\n"
        "                    backend='soundfile',\n"
        "                )",
        "                # PATCH (PosterChanAI): soundfile instead of torchaudio.save (torchcodec-free).\n"
        "                import soundfile as _sf\n"
        "                _sf.write(str(output_path), audio_tensor.transpose(0, 1).cpu().numpy(), sample_rate)",
    ),
]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: acestep_soundfile_patch.py <ACE-Step dir>", file=sys.stderr)
        return 2
    f = pathlib.Path(sys.argv[1]) / "acestep" / "audio_utils.py"
    if not f.exists():
        print(f"[patch] WARN: {f} not found — skipping", file=sys.stderr)
        return 0
    s = f.read_text()
    if "PATCH (PosterChanAI)" in s:
        print("[patch] already applied")
        return 0
    total = 0
    for old, new in REPLACEMENTS:
        n = s.count(old)
        total += n
        s = s.replace(old, new)
    f.write_text(s)
    print(f"[patch] applied soundfile save to audio_utils.py ({total} block(s))"
          if total else "[patch] WARN: no torchaudio.save blocks matched (upstream changed?)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
