"""os/firefox-theme/amo.py against a fake addons.mozilla.org.

"Make sure to publish the theme to Mozilla" — 1.0.0 was signed UNLISTED (no store page). amo.py
submits a LISTED version and must be resumable, because a listed version is only signed after
Mozilla's review and a CI job can end before that: a second run must not re-upload (AMO refuses a
version that exists) but wait for, and fetch, the signed file.
"""
import base64
import hashlib
import hmac
import importlib.util
import io
import json
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("pc_amo", ROOT / "os" / "firefox-theme" / "amo.py")
amo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(amo)

GUID, VER = "cyberpunk-theme@poster.place", "9.9.9"
SECRET = "s3cret"


def _xpi(signed=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"version": VER, "homepage_url": "https://poster.place",
                                                "browser_specific_settings": {"gecko": {"id": GUID}}}))
        if signed:
            z.writestr("META-INF/mozilla.rsa", b"sig")
    return buf.getvalue()


class FakeAMO:
    def __init__(self, existing=None, valid=True, approve_after=1):
        self.version = existing          # None = the version does not exist yet
        self.valid, self.approve_after, self.gets = valid, approve_after, 0
        self.calls, self.auth_ok = [], True
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self):
                tok = self.headers.get("Authorization", "")[4:]
                head, body, sig = tok.split(".")
                want = hmac.new(SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
                pad = sig + "=" * (-len(sig) % 4)
                if base64.urlsafe_b64decode(pad) != want:
                    fake.auth_ok = False

            def _send(self, code, obj=None, raw=None):
                data = raw if raw is not None else json.dumps(obj or {}).encode()
                self.send_response(code); self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_GET(self):
                self._auth(); fake.calls.append(("GET", self.path))
                if self.path.startswith("/api/v5/addons/addon/%s/versions/v%s/" % (GUID, VER)):
                    if fake.version is None:
                        return self._send(404, {"detail": "Not found."})
                    fake.gets += 1
                    if fake.gets >= fake.approve_after and fake.version["file"]["status"] == "unreviewed":
                        fake.version["file"] = {"status": "public", "url": fake.base + "/dl/theme.xpi"}
                    return self._send(200, fake.version)
                if self.path.startswith("/api/v5/addons/upload/"):
                    return self._send(200, {"uuid": "u1", "processed": True, "valid": fake.valid,
                                            "validation": {"errors": 0 if fake.valid else 1}})
                if self.path == "/dl/theme.xpi":
                    return self._send(200, raw=_xpi(signed=True))
                self._send(404)

            def do_POST(self):
                self._auth(); body = self._body(); fake.calls.append(("POST", self.path, body))
                if self.path == "/api/v5/addons/upload/":
                    return self._send(201, {"uuid": "u1", "processed": False})
                if self.path == "/api/v5/addons/addon/%s/versions/" % GUID:
                    fake.version = {"version": VER, "file": {"status": "unreviewed"}}
                    return self._send(201, fake.version)
                self._send(404)

            def do_PATCH(self):
                self._auth(); fake.calls.append(("PATCH", self.path, self._body()))
                self._send(200, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class Publish(unittest.TestCase):
    def run_it(self, fake, tmp, timeout=100):
        xpi = Path(tmp) / "t.xpi"; xpi.write_bytes(_xpi())
        out = Path(tmp) / "signed.xpi"
        t = [0.0]

        def clock():
            t[0] += 10
            return t[0]
        client = amo.AMO("issuer", SECRET, api=fake.base + "/api/v5")
        rc = amo.publish(client, str(xpi), str(out), timeout=timeout, poll=0, sleep=lambda s: None, clock=clock)
        return rc, out

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def test_a_new_version_is_submitted_LISTED_and_the_signed_file_fetched(self):
        fake = FakeAMO(approve_after=2); self.addCleanup(fake.close)
        rc, out = self.run_it(fake, self.tmp.name)
        self.assertEqual(rc, 0)
        self.assertTrue(amo.is_signed(out))
        upload = [c for c in fake.calls if c[0] == "POST" and c[1] == "/api/v5/addons/upload/"]
        self.assertEqual(len(upload), 1)
        self.assertIn(b'name="channel"\r\n\r\nlisted', upload[0][2], "must be the PUBLIC listing, not unlisted")
        patch = [c for c in fake.calls if c[0] == "PATCH"]
        self.assertTrue(patch and b'"categories"' in patch[0][2] and b'"summary"' in patch[0][2])
        ver = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/versions/")]
        self.assertIn(b'"license"', ver[0][2])
        self.assertTrue(fake.auth_ok, "the JWT must verify with the AMO secret")

    def test_a_pending_review_is_reported_and_a_rerun_does_not_upload_again(self):
        fake = FakeAMO(existing={"version": VER, "file": {"status": "unreviewed"}}, approve_after=10**6)
        self.addCleanup(fake.close)
        rc, out = self.run_it(fake, self.tmp.name, timeout=30)
        self.assertEqual(rc, amo.PENDING)
        self.assertFalse(out.exists())
        self.assertFalse([c for c in fake.calls if c[0] == "POST"], "an existing version must never be re-uploaded")

    def test_a_rerun_after_approval_fetches_it(self):
        fake = FakeAMO(existing={"version": VER, "file": {"status": "unreviewed"}}, approve_after=1)
        self.addCleanup(fake.close)
        rc, out = self.run_it(fake, self.tmp.name)
        self.assertEqual(rc, 0)
        self.assertTrue(amo.is_signed(out))

    def test_a_validation_failure_is_an_error(self):
        fake = FakeAMO(valid=False); self.addCleanup(fake.close)
        rc, out = self.run_it(fake, self.tmp.name)
        self.assertEqual(rc, 1)
        self.assertFalse([c for c in fake.calls if c[0] == "POST" and c[1].endswith("/versions/")])

    def test_the_workflow_uses_it_and_is_scheduled(self):
        wf = (ROOT / ".github/workflows/firefox-theme.yml").read_text()
        self.assertIn("os/firefox-theme/amo.py", wf)
        self.assertNotIn("--channel unlisted", wf)
        self.assertIn("schedule:", wf)
        self.assertIn("steps.amo.outputs.ready == 'yes'", wf)


if __name__ == "__main__":
    unittest.main()


class Unlisted(Publish):
    def test_unlisted_is_signed_without_touching_the_listing(self):
        """1.1.0 sat in the LISTED review queue for days while PosterChanOS kept shipping the old
        purple theme. The version PosterChanOS installs goes UNLISTED (signed within minutes)."""
        fake = FakeAMO(approve_after=1); self.addCleanup(fake.close)
        xpi = Path(self.tmp.name) / "t.xpi"; xpi.write_bytes(_xpi())
        out = Path(self.tmp.name) / "signed.xpi"
        client = amo.AMO("issuer", SECRET, api=fake.base + "/api/v5")
        t = [0.0]
        def clock():
            t[0] += 10; return t[0]
        rc = amo.publish(client, str(xpi), str(out), timeout=100, poll=0, sleep=lambda s: None,
                         clock=clock, channel="unlisted")
        self.assertEqual(rc, 0)
        up = [c for c in fake.calls if c[0] == "POST" and c[1] == "/api/v5/addons/upload/"]
        self.assertIn(b'name="channel"\r\n\r\nunlisted', up[0][2])
        self.assertFalse([c for c in fake.calls if c[0] == "PATCH"], "unlisted must not edit the public listing")

    def test_the_workflow_reads_the_recorded_channel(self):
        wf = (ROOT / ".github/workflows/firefox-theme.yml").read_text()
        self.assertIn("os/firefox-theme/amo-channel", wf)
        self.assertIn('--channel "${ch:-listed}"', wf)
        self.assertEqual((ROOT / "os/firefox-theme/amo-channel").read_text().strip(), "unlisted")
