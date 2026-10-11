"""Download/Save goes to the phone; only SHARE buttons open the share sheet.

2026-10-10: "when you hit download it should save automatically not go into share & ask how you want to share
it because that's not downloading". Every Download in the APK went through saveBlobAs, which WAS the share
sheet. saveBlobAs now saves through MediaSave and keeps the sheet only as its fallback
(tests/client/test_ai_download_native.py runs that). This keeps a NEW save from being written straight onto
the share sheet again: a direct share-sheet call is allowed only in the places whose button says Share or
Open with, each named here with what it is.
"""
import re
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[2] / "static" / "js" / "client"
CALL = re.compile(r"(?:\bShare\.share|navigator\.share)\s*\(")

ALLOWED = {
    "upload.js": "saveBlobAs's fallback when the phone cannot save directly",
    "lightbox.js": "the image viewer's Share and Open with… buttons",
    "preview.js": "the file preview's Share button",
    "contacts.js": "Share contact",
}


def test_only_share_buttons_call_the_share_sheet():
    offenders = []
    for f in sorted(CLIENT.glob("*.js")):
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in CALL.finditer(text):
            if f.name not in ALLOWED:
                line = text.count("\n", 0, m.start()) + 1
                offenders.append("%s:%d" % (f.name, line))
    assert not offenders, ("the share sheet is opened directly here -- a Download/Save must call "
                           "saveBlobAs (it saves to the phone and falls back to the sheet): %s" % offenders)


def test_save_blob_as_tries_the_phone_before_the_sheet():
    src = (CLIENT / "upload.js").read_text(encoding="utf-8")
    body = src[src.index("async function saveBlobAs("):]
    body = body[:body.index("\n  }\n") + 4]
    assert "MediaSave" in body and "Share.share" in body
    assert body.index("MediaSave") < body.index("Share.share"), "the share sheet is tried before saving"
