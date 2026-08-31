"""Save as PDF, done by us because Collabora's own cannot work here.

CODE's File > Download as > PDF converts server-side and then hands the browser a DOWNLOAD from
inside a cross-origin iframe. The desktop shell and the APK both refuse that — the same reason
nothing in this app uses a bare `<a download>` — so the menu item ran a real conversion and then
saved nothing, with no error anywhere. Reported as "I clicked Save as a pdf and nothing happened".

`/client/office/session/{id}/export/{fmt}` is the same conversion asked for by us, so the bytes come
back to the client and go out through `saveBlobAs`, which works in a browser, in Electron and in the
WebView. This file covers the parts that can be wrong in silence: the allowlist (this endpoint is
otherwise a general-purpose conversion service behind one session token), the token, the filename
header, and — when a CODE is actually reachable — that the conversion produces a real PDF.
"""
import os
import re
import unittest

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import office


def _app() -> TestClient:
    api = FastAPI()
    api.include_router(office.router)
    return TestClient(api)


class ExportIsGated(unittest.TestCase):
    def setUp(self):
        self.client = _app()
        self.session = "0123456789abcdef" * 2      # _safe_id demands 32 hex characters
        d = office._dir(self.session)
        d.mkdir(parents=True, exist_ok=True)
        (d / "document").write_bytes(b"hello pdf test\n")
        (d / "meta.json").write_text(
            '{"name": "notes.txt", "size": 15, "version": 1, "readonly": false,'
            ' "expires": %d}' % (int(__import__("time").time()) + 3600), encoding="utf-8")
        self.token = office._token(self.session, int(__import__("time").time()) + 3600)

    def tearDown(self):
        import shutil
        shutil.rmtree(office._dir(self.session), ignore_errors=True)

    def test_an_unlisted_format_is_refused(self):
        """Without the allowlist this is a conversion service for any format LibreOffice knows,
        reachable by anyone holding one session token."""
        r = self.client.get(f"/client/office/session/{self.session}/export/exe",
                            params={"access_token": self.token})
        self.assertEqual(r.status_code, 415)

    def test_a_wrong_token_cannot_convert_someone_elses_document(self):
        r = self.client.get(f"/client/office/session/{self.session}/export/pdf",
                            params={"access_token": "not-the-token"})
        self.assertIn(r.status_code, (401, 403, 404))

    def test_the_client_asks_for_a_format_the_server_offers(self):
        """The button and the allowlist are in different files and different languages."""
        from pathlib import Path
        app_js = (Path(__file__).resolve().parent.parent / "static" / "js" / "client"
                  / "app.js").read_text(encoding="utf-8")
        asked = set(re.findall(r"/client/office/session/'\+session\.id\+'/export/(\w+)", app_js))
        self.assertTrue(asked, "no client asks for an export at all")
        self.assertTrue(asked <= set(office._EXPORT),
                        f"the client asks for {asked - set(office._EXPORT)}, which the server refuses")

    def test_the_filename_header_cannot_be_broken_by_the_document_name(self):
        """The name comes from an uploaded file, so a quote or a newline in it would either break
        the header or let the response claim a different filename."""
        self.assertEqual(office._safe_name('a"b\nc/d'), "a_b_c_d")
        self.assertEqual(office._safe_name(""), "document")
        self.assertLessEqual(len(office._safe_name("x" * 500)), 120)


class ExportActuallyConverts(unittest.TestCase):
    """Runs only where a CODE is reachable. Nine tests of rejections passing while the happy path
    was broken is exactly how the last office bug shipped, so this one asks the real server."""

    @classmethod
    def setUpClass(cls):
        cls.base = None
        for root in (office._SERVICE_ROOT, ""):
            try:
                r = httpx.get(f"{office._CODE}{root}/hosting/discovery", timeout=5)
                if r.status_code == 200:
                    cls.base = f"{office._CODE}{root}"
                    break
            except Exception:
                continue
        if cls.base is None:
            raise unittest.SkipTest(f"no CODE server at {office._CODE} — nothing was converted")

    def test_convert_to_pdf_returns_a_pdf(self):
        r = httpx.post(f"{self.base}/cool/convert-to/pdf",
                       files={"data": ("notes.txt", b"hello pdf test\n", "text/plain")},
                       timeout=120)
        self.assertEqual(r.status_code, 200, r.text[:400])
        self.assertTrue(r.content.startswith(b"%PDF-"),
                        "the converter answered 200 with something that is not a PDF")
        self.assertGreater(len(r.content), 500)


if __name__ == "__main__":
    unittest.main()
