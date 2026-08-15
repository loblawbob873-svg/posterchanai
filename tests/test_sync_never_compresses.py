"""A synced file is a COPY OF YOUR FILE. Nothing on the way to the relay may re-encode it.

Run: venv-unified/bin/python -m pytest tests/test_sync_never_compresses.py

`uploadBlob` prepares media OPT-OUT:

    if(!(opts && opts.noCompress)) file=await compressMedia(file);

which is right for a photo going to a timeline — a 6 MB phone picture does not need to reach a feed
at full size — and catastrophic for "keep this file". compressImage re-encodes anything over 800 KB
or 2560px to JPEG at a quality as low as 0.45 and drops EXIF with it; compressVideo hands the clip to
the node's ffmpeg. Either one means the bytes stored are not the bytes read, the sha no longer
identifies the file it claims to be a copy of, and every other device pulls down the re-encoded one.

THE UPLOADS BELOW WERE SAFE ONLY BY ACCIDENT. They hand uploadBlob ciphertext named `sync.enc` /
`<name>.enc` with type `application/octet-stream`, so compressImage bails on `!/^image\\//` and
`_isVideoFile` finds no video extension — a property of NAMING, not a decision anybody took. Give a
chunk a truthful name or mime one day (for the X-Filename header, for a nicer progress line) and the
app silently starts re-encoding people's video, with nothing in any log and no way to get it back.

So the flag is passed explicitly, and this test is what keeps it there.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")


def _upload_calls():
    """Every uploadBlob(...) call site, as (line number, the options object text)."""
    out = []
    for m in re.finditer(r"uploadBlob\(", APP):
        # Balance parens from the call so a File(...) argument doesn't end the match early.
        i, depth = m.end(), 1
        while i < len(APP) and depth:
            if APP[i] == "(":
                depth += 1
            elif APP[i] == ")":
                depth -= 1
            i += 1
        out.append((APP.count("\n", 0, m.start()) + 1, APP[m.end(): i - 1]))
    return out


def test_every_encrypted_drive_upload_opts_out_of_compression():
    """The five that store somebody's actual bytes: folder sync (whole-file AND per-chunk), the
    encrypted drive, a Notes attachment, and a music track."""
    keepers = [(ln, args) for ln, args in _upload_calls() if "keep:true" in args.replace(" ", "")]
    assert len(keepers) >= 5, f"expected the encrypted-drive uploads to still be here, got {len(keepers)}"
    bad = [ln for ln, args in keepers if "noCompress:true" not in args.replace(" ", "")]
    assert not bad, (
        f"uploadBlob(..., keep:true) without noCompress at line(s) {bad} — a kept blob is a copy of a "
        "file, and uploadBlob compresses unless told not to")


def test_the_gate_is_still_opt_out_so_this_test_still_matters():
    """If uploadBlob is ever changed to compress opt-IN, the assertion above becomes vacuous rather
    than false. Pin the shape it is guarding against."""
    assert "if(!(opts && opts.noCompress)) file=await compressMedia(file);" in APP, (
        "uploadBlob's compression gate moved — re-read this file before trusting the test above")


def test_the_accidental_safety_is_documented_where_it_would_be_removed():
    """The naming is load-bearing today, and the next person to 'improve' a chunk's filename has to
    meet that in the source rather than discover it from a corrupted video.

    Anchored on the sentence itself rather than on a byte offset from a neighbouring function — the
    first version sliced 4000 characters backwards from putParts and went red the moment anything
    between them changed size, which is a test that fails for the wrong reason."""
    assert "uploadBlob compresses OPT-OUT" in APP, (
        "the note explaining why a sync chunk escapes compressMedia is gone")
    assert "accident of naming, not a decision" in APP


def test_a_sync_chunk_is_never_given_a_media_name_or_mime():
    """The other half of the same guarantee: even with the flag, a chunk that looked like a video
    would be handed to compressVideo by anything else that prepares uploads."""
    m = re.search(r"async putParts\(readPart, size, onProgress, chunkBytes\)\{.*?\n      \},", APP, re.S)
    assert m, "putParts moved — re-point this test"
    body = m.group(0)
    assert "'sync.enc'" in body, "a chunk is no longer named sync.enc"
    assert "application/octet-stream" in body
    assert not re.search(r"new File\(\[[^\]]*\],\s*[^,]*\.name", body), (
        "a chunk is being named after the real file — compressVideo keys on the extension")
