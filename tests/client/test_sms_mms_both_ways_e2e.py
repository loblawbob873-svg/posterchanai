"""PICTURE MESSAGES BOTH WAYS, END TO END: EACH SIDE'S OWN OUTPUT HANDED TO THE OTHER SIDE'S OWN CODE.

2026-10-22, the same gap as tests/client/test_sms_send_desktop_to_phone_e2e.py: tests/client/
test_sms_attachments.py and test_sms_sent_picture_shown_once.py drive sms.js with JS-shaped picture
documents, and nothing had ever handed the desktop a picture message the PHONE built, or the phone a
picture request the DESKTOP wrote.

(a) INCOMING: the phone's archive of a received MMS is built by the SHIPPED `SmsSweep.run`, sealed the
    way `SmsArchive.sweep` seals it, its attachment stored as real drive ciphertext (`SyncCrypto`)
    (tests/sms_java_phone.py `sweep`). The SHIPPED sms.js (real key, real NIP-44) receives it live --
    from two relays -- and must show ONE message carrying the picture.
(b) OUTGOING: the desktop sends a photo. Its `pcai:smsout:` request is handed to the SHIPPED
    `SmsOutbox.perform`, whose MMS branch runs the REAL `SyncStore`, `SyncNet` (HTTP, to a local server
    standing in for the instance's files index and Blossom) and `SyncCrypto` -- the radio is the only
    recorder. Then the phone's own archive of the SENT row (its provider re-encoded the photo, as
    phones do) arrives, and the desktop shows the picture ONCE and not "sending".
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
import sms_java_phone  # noqa: E402
from app.services.nostr.nip44 import encrypt_self  # noqa: E402

SIM = ROOT / "tests/client/sms_sim.js"
SEC = "01" * 32
TO = "+15550142"
MK = bytes(range(32))
PHOTO = bytes((i * 37 + 11) & 0xFF for i in range(4096))        # what the person picked
SENT_COPY = bytes((i * 13 + 5) & 0xFF for i in range(3100))     # what the phone's MMS stack stored

pytestmark = pytest.mark.skipif(not sms_java_phone.available(), reason="JDK + android.jar required")


@pytest.fixture(scope="module")
def phone():
    p = sms_java_phone.Phone()
    yield p
    p.close()


def sim(**opt):
    r = subprocess.run(["node", str(SIM), json.dumps(opt)], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-4000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def seal(plain):
    """The drive's v2 blob: 12-byte IV, then AES-GCM under the master key (files.js _masterEncrypt)."""
    iv = os.urandom(12)
    return iv + AESGCM(MK).encrypt(iv, plain, None)


def b64(b):
    return base64.b64encode(b).decode()


@pytest.fixture()
def instance():
    """This instance as the phone's SyncNet sees it: the files index (the wrapped drive key) and Blossom."""
    blobs, asked = {}, []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            asked.append(("POST", self.path))
            if self.path == "/client/files-index":
                wrapped = encrypt_self(bytes.fromhex(SEC), json.dumps({"k": b64(MK)}))
                return self._send(200, json.dumps({"ok": True, "index": {"mk": wrapped}}).encode(), "application/json")
            self._send(404, b"{}", "application/json")

        def do_GET(self):
            asked.append(("GET", self.path))
            sha = self.path.split("/")[-1].split("?")[0]
            if self.path.startswith("/blossom/") and sha in blobs:
                return self._send(200, blobs[sha], "application/octet-stream")
            self._send(404, b"", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    yield {"api": base, "media": base + "/blossom", "blobs": blobs, "asked": asked}
    srv.shutdown()


def test_a_picture_the_phone_received_is_shown_once_on_the_desktop(phone, tmp_path):
    state = str(tmp_path / "phone.json")
    # ONE provider row, archived on two passes (two relays' worth of the same document).
    sweep = {"mk": b64(MK), "rows": [{"address": TO, "body": "look", "date": int(time.time() * 1000), "type": 1, "mms": True,
                                       "parts": [{"ct": "image/jpeg", "name": "IMG_1.jpg", "b64": b64(PHOTO)}]}]}
    out = sim(realKey=SEC, isPhone=False, telephony=False, sweepArgv=phone.argv("sweep", SEC, state),
              sweepInput=sweep, steps=["load", "phoneSweep", "phoneSweep"])
    sw = out["phone"][0]["sweep"]
    assert sw["events"] == 1 and sw["attachments"] == 1 and not sw["error"], sw
    t = out["threads"]
    assert len(t) == 1 and t[0]["bodies"] == ["look"], t
    assert t[0]["parts"] == [1], "the desktop drew the message without its picture: %s" % t
    sha = t[0]["partShas"][0][0]
    # The hash the desktop will fetch is the phone's real drive ciphertext, which opens to the photo.
    blob = base64.b64decode(out["uploads"][sha]["b64"])
    assert hashlib.sha256(blob).hexdigest() == sha
    assert AESGCM(MK).decrypt(blob[:12], blob[12:], None) == PHOTO


def desktop_sends_a_photo(phone, tmp_path, instance, then=()):
    state = str(tmp_path / "phone.json")
    # What the WebView's SyncPlugin.configure leaves on a signed-in phone: where the instance and its
    # Blossom are, and the account's wrapped drive key (a background MMS needs all three).
    phone.run("configure", SEC, state, {"api": instance["api"], "media": instance["media"],
                                        "mk": encrypt_self(bytes.fromhex(SEC), json.dumps({"k": b64(MK)}))})
    # Where the desktop's upload lands: its drive address (the sim's uploadEncFile names it by content).
    instance["blobs"][hashlib.sha256(PHOTO).hexdigest()] = seal(PHOTO)
    return sim(realKey=SEC, isPhone=False, telephony=False, fileB64=b64(PHOTO), fileName="photo.jpg",
               phoneArgv=phone.argv("perform", SEC, state), phoneRelays=2,
               sweepArgv=phone.argv("sweep", SEC, state),
               sweepInput={"mk": b64(MK), "rows": [{"address": TO, "body": "for you", "date": "now", "type": 2,
                                                     "mms": True, "parts": [{"ct": "image/jpeg", "name": "image.jpg",
                                                                             "b64": b64(SENT_COPY)}]}]},
               steps=["load", "sendfile:%s:%d:for you" % (TO, len(PHOTO)), "phone"] + list(then))


def test_a_picture_sent_from_the_desktop_goes_out_as_one_mms_of_the_picked_bytes(phone, tmp_path, instance):
    out = desktop_sends_a_photo(phone, tmp_path, instance)
    assert ["sendFileResult", True, "queued", ""] in out["calls"], out["calls"][-6:]
    run = out["phone"][0]
    assert len(run["radio"]) == 1, "one request from two relays must be one MMS: %s" % run["radio"]
    mms = run["radio"][0]
    assert mms["kind"] == "mms" and mms["to"] == TO and mms["body"] == "for you", mms
    assert base64.b64decode(mms["b64"]) == PHOTO, "the phone sent different bytes from the ones picked"
    assert mms["mime"] == "image/jpeg" and mms["name"] == "photo.jpg", mms
    # It fetched exactly the address the request named, with the account's drive key from the index.
    assert ("GET", "/blossom/" + hashlib.sha256(PHOTO).hexdigest()) in instance["asked"], instance["asked"]
    # Android ACCEPTED an asynchronous MMS: the bubble stays honest ("sending"), with its picture.
    t = out["threads"][0]
    assert t["parts"] == [1] and t["pending"] == [True] and t["failed"] == [False], t


def test_the_phones_archive_of_that_mms_settles_it_shown_once_and_sent(phone, tmp_path, instance):
    out = desktop_sends_a_photo(phone, tmp_path, instance, then=["phoneSweep"])
    assert len(out["phone"][0]["radio"]) == 1, out["phone"]
    assert out["phone"][1]["sweep"]["events"] == 1, out["phone"]
    t = out["threads"][0]
    assert t["parts"] == [1], "the picture is shown %d times: %s" % (sum(t["parts"]), t)
    assert t["pending"] == [False] and t["failed"] == [False], t
