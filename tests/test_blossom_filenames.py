"""Blossom blobs carry a FILE EXTENSION and (when the uploader sent one) a filename.

Run: venv-unified/bin/python -m unittest tests.test_blossom_filenames

A Blossom blob is addressed by its sha256, so on its own it has no name and no type — which is how
`https://poster.place/blossom/<64 hex>` ended up being what users downloaded: a file their OS could
not open and no other client would render inline. BUD-02 says the descriptor `url` must carry the
extension when it's known, and the uploader's original filename is the only place a real name can
come from, so we keep it per-owner (dedup means one set of bytes can be two files to two people).

The sanitiser is the security half: that string is echoed into a `Content-Disposition` header and
into a save dialog, so a path, a quote or a newline in it would be header injection / path escape.
"""
import unittest
from unittest import mock

from app.services import blossom_service


def _blob(mime="image/png", sha="a" * 64):
    return mock.Mock(sha256=sha, size=123, mime=mime, created_at=0)


class TestExtForMime(unittest.TestCase):
    def test_common_types(self):
        for mime, ext in (("image/png", "png"), ("image/jpeg", "jpg"), ("video/mp4", "mp4"),
                          ("application/pdf", "pdf"), ("audio/ogg", "ogg"), ("video/quicktime", "mov")):
            self.assertEqual(blossom_service.ext_for_mime(mime), ext, mime)

    def test_parameters_and_case_are_ignored(self):
        self.assertEqual(blossom_service.ext_for_mime("IMAGE/PNG; charset=binary"), "png")

    def test_unknown_and_generic_types_have_no_extension(self):
        """Better a bare hash than a WRONG extension — octet-stream says nothing about the bytes."""
        for mime in ("", "application/octet-stream", "application/x-made-up-thing"):
            self.assertEqual(blossom_service.ext_for_mime(mime), "")


class TestDescriptorUrl(unittest.TestCase):
    def test_url_carries_the_extension(self):
        d = blossom_service.descriptor(_blob("image/png"), "https://m.example/blossom")
        self.assertEqual(d["url"], "https://m.example/blossom/" + "a" * 64 + ".png")

    def test_unknown_type_stays_extensionless(self):
        d = blossom_service.descriptor(_blob("application/octet-stream"), "https://m.example/blossom")
        self.assertEqual(d["url"], "https://m.example/blossom/" + "a" * 64)
        self.assertNotIn("name", d)

    def test_uploaders_own_extension_wins_over_the_mime_guess(self):
        """The user uploaded `holiday.jpeg`; handing them back `.jpg` renames their file."""
        d = blossom_service.descriptor(_blob("image/jpeg"), "https://m.example/blossom", name="holiday.jpeg")
        self.assertTrue(d["url"].endswith(".jpeg"))
        self.assertEqual(d["name"], "holiday.jpeg")

    def test_a_name_without_an_extension_still_gets_one_from_the_mime(self):
        d = blossom_service.descriptor(_blob("application/pdf"), "https://m.example/blossom", name="invoice")
        self.assertTrue(d["url"].endswith(".pdf"))
        self.assertEqual(d["name"], "invoice")

    def test_a_dotted_name_that_is_not_an_extension_falls_back_to_the_mime(self):
        d = blossom_service.descriptor(_blob("image/png"), "https://m.example/blossom", name="v1.2.3 render")
        self.assertTrue(d["url"].endswith(".png"), d["url"])


class TestSafeFilename(unittest.TestCase):
    def test_a_path_is_reduced_to_its_basename(self):
        self.assertEqual(blossom_service.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(blossom_service.safe_filename(r"C:\Users\me\report.pdf"), "report.pdf")

    def test_header_breaking_characters_are_dropped(self):
        """This string goes inside `Content-Disposition: inline; filename="…"`."""
        # everything up to the last "/" is path, so only the trailing segment survives — quote gone
        self.assertEqual(blossom_service.safe_filename('a"; rm -rf /;b.txt'), "b.txt")
        self.assertEqual(blossom_service.safe_filename("evil\r\nX-Injected: 1.png"), "evilX-Injected: 1.png")

    def test_leading_dots_and_length(self):
        self.assertEqual(blossom_service.safe_filename("...hidden"), "hidden")
        self.assertLessEqual(len(blossom_service.safe_filename("x" * 500 + ".png")), 120)

    def test_empty_input(self):
        self.assertEqual(blossom_service.safe_filename(""), "")
        self.assertEqual(blossom_service.safe_filename(None), "")


class TestUploadFilenameHeader(unittest.TestCase):
    """BUD-01 has no filename field, so the client sends one out of band — best-effort, never required."""

    def _req(self, headers):
        return type("R", (), {"headers": headers})()

    def test_plain_and_percent_encoded_x_filename(self):
        from app.routers import blossom as blossom_router
        self.assertEqual(blossom_router._upload_filename(self._req({"x-filename": "report.pdf"})), "report.pdf")
        # non-ASCII can't ride in a raw header, so the client percent-encodes it
        self.assertEqual(blossom_router._upload_filename(self._req({"x-filename": "r%C3%A9sum%C3%A9.pdf"})),
                         "résumé.pdf")

    def test_content_disposition_fallback(self):
        from app.routers import blossom as blossom_router
        r = self._req({"content-disposition": 'form-data; name="file"; filename="holiday snap.jpg"'})
        self.assertEqual(blossom_router._upload_filename(r), "holiday snap.jpg")

    def test_missing_is_empty_not_an_error(self):
        from app.routers import blossom as blossom_router
        self.assertEqual(blossom_router._upload_filename(self._req({})), "")

    def test_a_traversal_attempt_in_the_header_is_sanitised(self):
        from app.routers import blossom as blossom_router
        self.assertEqual(blossom_router._upload_filename(self._req({"x-filename": "%2e%2e%2f%2e%2e%2fshadow"})),
                         "shadow")


class TestSniffExt(unittest.TestCase):
    """The last resort for a blob whose stored MIME is application/octet-stream — which is what a
    client that didn't set Content-Type uploads. Such a blob has no type, no URL extension and no
    name, so its magic number is the only thing that can name the download."""

    def test_containers_and_documents(self):
        cases = [
            (b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4, "mp4"),
            (b"\x1a\x45\xdf\xa3" + b"\x00" * 12, "webm"),
            (b"OggS" + b"\x00" * 12, "ogg"),
            (b"%PDF-1.7" + b"\x00" * 8, "pdf"),
            (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "png"),
            (b"\xff\xd8\xff\xe0" + b"\x00" * 12, "jpg"),
            (b"PK\x03\x04" + b"\x00" * 12, "zip"),
        ]
        for head, ext in cases:
            self.assertEqual(blossom_service.sniff_ext(head), ext, ext)

    def test_riff_needs_the_tag_at_byte_8(self):
        self.assertEqual(blossom_service.sniff_ext(b"RIFF\x00\x00\x00\x00WEBPxxxx"), "webp")
        self.assertEqual(blossom_service.sniff_ext(b"RIFF\x00\x00\x00\x00WAVEfmt "), "wav")
        self.assertEqual(blossom_service.sniff_ext(b"RIFF\x00\x00\x00\x00NOPExxxx"), "")

    def test_unknown_bytes_and_short_reads_are_empty_not_a_guess(self):
        self.assertEqual(blossom_service.sniff_ext(b"just some text here"), "")
        self.assertEqual(blossom_service.sniff_ext(b""), "")
        self.assertEqual(blossom_service.sniff_ext(b"\x00"), "")


class TestContentDisposition(unittest.TestCase):
    """The header the browser's save dialog reads. It is encoded latin-1 by the ASGI layer, so a
    non-ASCII name put straight into it raises UnicodeEncodeError — i.e. a 500 instead of a file."""

    def test_ascii_name_is_a_plain_disposition(self):
        from app.routers import blossom as blossom_router
        self.assertEqual(blossom_router._disposition("attachment", "report.pdf"),
                         'attachment; filename="report.pdf"')

    def test_non_ascii_name_gets_an_rfc5987_form_and_stays_latin1_encodable(self):
        from app.routers import blossom as blossom_router
        v = blossom_router._disposition("attachment", "写真.png")
        self.assertIn("filename*=UTF-8''", v)
        v.encode("latin-1")   # would raise before the RFC 5987 split — this is the actual regression

    def test_an_all_non_ascii_name_still_has_an_ascii_fallback(self):
        from app.routers import blossom as blossom_router
        v = blossom_router._disposition("attachment", "写真")
        self.assertIn('filename="download"', v)
        v.encode("latin-1")


if __name__ == "__main__":
    unittest.main()
