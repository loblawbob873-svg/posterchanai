"""A media upload that falls back from /api/v2/media to /api/v1/media must send the WHOLE file again.

Measured 2026-09-24 on detroitriotcity.com: the blockbot's post carried an attachment of ZERO bytes
(sha256 e3b0c442..., `inode/x-empty`). One BytesIO was shared by both attempts -- the v2 upload of the
7 MB block GIF read it to the end and then timed out, and the v1 retry sent the exhausted stream. It had
worked for months only because v2 had never failed. The GIF was also labelled image.png.

Runs the real botframework/pleroma.py from its own directory (as the bot does) with `requests.post`
replaced by a fake that READS what each attempt sends.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import json, sys
import requests
import pleroma

sent = []

class R:
    def __init__(self, code, body): self.status_code, self._b, self.text = code, body, json.dumps(body)
    def json(self): return self._b

def post(url, headers=None, files=None, timeout=None):
    name, stream, mime = files["file"]
    data = stream.read()                       # what actually goes over the wire
    sent.append({"url": url, "len": len(data), "name": name, "mime": mime})
    if url.endswith("/api/v2/media") and MODE == "v2-timeout":
        raise requests.exceptions.Timeout("read timed out")
    return R(200, {"id": "m1"})

MODE = sys.argv[1]
pleroma.requests.post = post
gif = b"GIF89a" + b"\x00" * 5000
arg = b"" if MODE == "empty" else gif
print(json.dumps({"id": pleroma.upload_media_to_pleroma(arg), "sent": sent}))
'''


def _run(mode):
    env = dict(os.environ, PLEROMA_ENDPOINT="https://pleroma.example", PLEROMA_ACCESS_TOKEN="t",
               PLEROMA_USERNAME="blockbot", PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-c", SCRIPT, mode], cwd=ROOT / "botframework", env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_v1_retry_after_a_v2_timeout_sends_the_whole_file():
    out = _run("v2-timeout")
    assert out["id"] == "m1"
    v2, v1 = out["sent"]
    assert v2["url"].endswith("/api/v2/media") and v1["url"].endswith("/api/v1/media")
    assert v1["len"] == v2["len"] == 5006, "the retry uploaded an exhausted stream: %r" % out


def test_a_gif_is_uploaded_as_a_gif():
    out = _run("ok")
    assert out["sent"][0]["mime"] == "image/gif" and out["sent"][0]["name"].endswith(".gif"), out


def test_nothing_is_uploaded_from_zero_bytes():
    out = _run("empty")
    assert out["id"] is None and out["sent"] == []
