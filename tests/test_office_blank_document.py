"""Office can CREATE a document, not only open one that already exists.

Reported as "a huge office gap, no way to create a new document by type", and it was exactly that:
`_officeSession` takes a file off the drive and hands it to CODE, and nothing anywhere made a file.
The only way to start a spreadsheet was to already have a spreadsheet.

WHAT THIS TEST CAN AND CANNOT PROVE. There is no LibreOffice or CODE on the build host or on the
desktop, so it cannot open the result and say "this works in the editor". It asserts the structural
rules an ODF reader relies on — the ones that are actually easy to get wrong when hand-building a
zip — and the docstring says so instead of implying more:

  * `mimetype` is the FIRST member and is STORED, not deflated. The ODF specification requires this
    so a reader can identify the type from the first bytes; a compressed or later-placed mimetype is
    the classic mistake that makes a valid-looking file open as a plain zip archive.
  * every member the manifest names is present, and the manifest's own media type matches.
  * content.xml and styles.xml are well-formed XML with the expected roots — a hand-written string
    is one missing angle bracket away from a document that opens empty or not at all.

ODF rather than OOXML on purpose: an ODF file is a zip of four small members, so this needs no new
dependency. A dependency added here is a node that will not start until somebody re-runs the
installer, and `sync.sh` deploys code, not deps.
"""
import io
import unittest
import xml.etree.ElementTree as ET
import zipfile

from app.routers.office import blank_document, _ODF_KINDS


class BlankDocumentsAreReadable(unittest.TestCase):
    def test_every_offered_kind_builds(self):
        self.assertEqual({"text", "spreadsheet", "presentation"}, set(_ODF_KINDS))

    def test_mimetype_is_first_and_stored(self):
        for kind in _ODF_KINDS:
            with self.subTest(kind=kind):
                data, ext, mime = blank_document(kind)
                z = zipfile.ZipFile(io.BytesIO(data))
                first = z.infolist()[0]
                self.assertEqual("mimetype", first.filename,
                                 "%s: mimetype must be the first member or a reader cannot "
                                 "identify the file from its first bytes" % kind)
                self.assertEqual(zipfile.ZIP_STORED, first.compress_type,
                                 "%s: a DEFLATED mimetype makes this open as a zip archive rather "
                                 "than as a document" % kind)
                self.assertEqual(mime, z.read("mimetype").decode())

    def test_the_manifest_names_only_members_that_exist(self):
        for kind in _ODF_KINDS:
            with self.subTest(kind=kind):
                data, _ext, mime = blank_document(kind)
                z = zipfile.ZipFile(io.BytesIO(data))
                names = set(z.namelist())
                root = ET.fromstring(z.read("META-INF/manifest.xml"))
                ns = "{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}"
                listed = [e.get(ns + "full-path") for e in root.findall(ns + "file-entry")]
                self.assertIn("/", listed, "the manifest must describe the document itself")
                for path in listed:
                    if path == "/":
                        continue
                    self.assertIn(path, names,
                                  "%s: the manifest names %r, which is not in the zip" % (kind, path))

    def test_the_xml_members_parse_and_have_the_right_roots(self):
        want = {"content.xml": "document-content", "styles.xml": "document-styles"}
        for kind in _ODF_KINDS:
            for member, tag in want.items():
                with self.subTest(kind=kind, member=member):
                    data, _e, _m = blank_document(kind)
                    z = zipfile.ZipFile(io.BytesIO(data))
                    root = ET.fromstring(z.read(member))     # raises on malformed XML
                    self.assertTrue(root.tag.endswith("}" + tag),
                                    "%s/%s has root %r" % (kind, member, root.tag))

    def test_the_extension_matches_the_type(self):
        self.assertEqual("odt", blank_document("text")[1])
        self.assertEqual("ods", blank_document("spreadsheet")[1])
        self.assertEqual("odp", blank_document("presentation")[1])

    def test_an_unknown_kind_is_refused_rather_than_guessed(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            blank_document("../etc/passwd")


if __name__ == "__main__":
    unittest.main()
