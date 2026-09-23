"""Android: "Open with PosterChan Office" for documents and PDFs, and a share to that entry.

Asked for as: "support sharing to and opening Office documents using PosterChan Office and being the
default app for opening office docs/PDFs". Android has no role for a document handler -- being the
default is the person picking "PosterChan Office" and "Always" in the Open-with sheet -- so what has
to be true is that the entry EXISTS for every type, carries a name they recognise, and does the right
thing once chosen:

  * DocIntent (the rule for what is ours and where it goes) is compiled and RUN: a .docx sent as
    `application/octet-stream` is still a document, a PDF goes to Preview, an image is not ours, and a
    PDF shared to plain "PosterChan" is still a file to POST;
  * the manifest's alias lists exactly DocIntent's types, for VIEW/EDIT and SEND, and is enabled;
  * OpenDocPlugin compiles against the REAL android.jar, is registered, and MainActivity counts each
    arrival so a resume is not a second open;
  * the page: a share to the Office entry is never also attached to a new post, a PDF opens in
    Preview, a document in the Office editor, and Save goes back to the file when Android allows it
    and otherwise saves a copy and SAYS so.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from tests import androidcompile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "mobile", "android", "app", "src", "main")
OFFICE = os.path.join(APP, "java", "place", "poster", "app", "office")
MANIFEST = os.path.join(APP, "AndroidManifest.xml")
JAVAC, JAVA_BIN = shutil.which("javac"), shutil.which("java")
A = "{http://schemas.android.com/apk/res/android}"

DRIVER = r"""
import place.poster.app.office.DocIntent;
public class Drive {
  static void say(String k, Object v) { System.out.println(k + "=" + v); }
  public static void main(String[] a) {
    say("docx", DocIntent.kindOf("application/vnd.openxmlformats-officedocument.wordprocessingml.document", null));
    say("octet-xlsx", DocIntent.kindOf("application/octet-stream", "Budget 2026.XLSX"));
    say("pdf", DocIntent.kindOf("application/pdf; charset=binary", ""));
    say("pdf-by-name", DocIntent.kindOf(null, "content://x/report.pdf?x=1"));
    say("odt", DocIntent.kindOf("application/vnd.oasis.opendocument.text", ""));
    say("image", DocIntent.kindOf("image/png", "photo.png"));
    say("noext", DocIntent.kindOf("", "README"));
    say("view", DocIntent.isOpen("android.intent.action.VIEW", false, "application/pdf", ""));
    say("edit", DocIntent.isOpen("android.intent.action.EDIT", false, "application/msword", ""));
    say("send-office", DocIntent.isOpen("android.intent.action.SEND", true, "application/pdf", ""));
    say("send-post", DocIntent.isOpen("android.intent.action.SEND", false, "application/pdf", ""));
    say("view-image", DocIntent.isOpen("android.intent.action.VIEW", false, "image/jpeg", ""));
    say("name", DocIntent.nameFor("../../evil:name?.docx", null, "office"));
    say("name-empty", DocIntent.nameFor("", "", "pdf"));
    say("name-path", DocIntent.nameFor(null, "Plans.odt", "office"));
    StringBuilder m = new StringBuilder(); for (String s : DocIntent.OFFICE_MIMES) m.append(s).append(',');
    say("mimes", m);
  }
}
"""


def _alias():
    root = ET.parse(MANIFEST).getroot()
    for el in root.iter("activity-alias"):
        if el.get(A + "name") == ".OpenInOffice":
            return el
    return None


@unittest.skipIf(not (JAVAC and JAVA_BIN), "no JDK on this node")
class DocIntentRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            drive = os.path.join(d, "Drive.java")
            open(drive, "w").write(DRIVER)
            r = subprocess.run([JAVAC, "-nowarn", "-d", d, os.path.join(OFFICE, "DocIntent.java"), drive],
                               capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, r.stderr
            run = subprocess.run([JAVA_BIN, "-cp", d, "Drive"], capture_output=True, text=True, timeout=60)
            assert run.returncode == 0, run.stderr
        cls.out = dict(line.split("=", 1) for line in run.stdout.splitlines())

    def test_documents_and_pdfs_are_recognised_by_type_or_by_name(self):
        o = self.out
        self.assertEqual((o["docx"], o["octet-xlsx"], o["pdf"], o["pdf-by-name"], o["odt"]),
                         ("office", "office", "pdf", "pdf", "office"))
        self.assertEqual((o["image"], o["noext"]), ("", ""))

    def test_a_pdf_shared_to_plain_posterchan_is_still_a_post(self):
        o = self.out
        self.assertEqual((o["view"], o["edit"], o["send-office"]), ("true", "true", "true"))
        self.assertEqual((o["send-post"], o["view-image"]), ("false", "false"))

    def test_a_name_cannot_carry_a_path(self):
        self.assertNotIn("/", self.out["name"])
        self.assertNotIn(":", self.out["name"])
        self.assertTrue(self.out["name"].endswith(".docx"))
        self.assertEqual(self.out["name-empty"], "document.pdf")
        self.assertEqual(self.out["name-path"], "Plans.odt")

    def test_the_manifest_offers_exactly_these_types(self):
        alias = _alias()
        self.assertIsNotNone(alias, "no PosterChan Office entry in the manifest")
        self.assertEqual(alias.get(A + "targetActivity"), ".MainActivity")
        self.assertEqual(alias.get(A + "label"), "PosterChan Office")
        self.assertEqual(alias.get(A + "exported"), "true")
        self.assertNotEqual(alias.get(A + "enabled"), "false", "the document handler ships disabled")
        want = set(filter(None, self.out["mimes"].split(","))) | {"application/pdf"}
        filters = alias.findall("intent-filter")
        actions = [{a.get(A + "name") for a in f.findall("action")} for f in filters]
        self.assertIn({"android.intent.action.VIEW", "android.intent.action.EDIT"}, actions)
        self.assertIn({"android.intent.action.SEND"}, actions)
        for f in filters:
            got = {d.get(A + "mimeType") for d in f.findall("data") if d.get(A + "mimeType")}
            self.assertEqual(got, want, "the manifest and DocIntent disagree about which types are ours")
            cats = {c.get(A + "name") for c in f.findall("category")}
            self.assertIn("android.intent.category.DEFAULT", cats, "without DEFAULT an implicit VIEW never matches")


@unittest.skipIf(not (JAVAC and androidcompile.android_jar()), "no javac / android.jar on this node")
class OpenDocCompiles(unittest.TestCase):
    def test_the_plugin_compiles_against_the_real_sdk(self):
        with tempfile.TemporaryDirectory() as d:
            r = androidcompile.compile_sources(
                [os.path.join(OFFICE, "DocIntent.java"), os.path.join(OFFICE, "OpenDocPlugin.java")], d)
        self.assertEqual(r.returncode, 0, r.stderr[-4000:])


class Wiring(unittest.TestCase):
    def test_main_activity_registers_the_plugin_and_counts_each_arrival(self):
        src = open(os.path.join(APP, "java", "place", "poster", "app", "MainActivity.java")).read()
        self.assertIn("registerPlugin(place.poster.app.office.OpenDocPlugin.class)", src)
        on_create = src[src.index("public void onCreate("):src.index("super.onCreate(savedInstanceState)")]
        self.assertIn("OpenDocPlugin.isDocIntent(getIntent())", on_create, "a cold open is never counted")
        on_new = src[src.index("public void onNewIntent("):]
        on_new = on_new[:on_new.index("super.onNewIntent(intent)")]
        self.assertIn("OpenDocPlugin.isDocIntent(intent)", on_new, "a warm open is never counted")

    def test_a_share_to_the_office_entry_is_not_also_a_post(self):
        st = open(os.path.join(APP, "java", "place", "poster", "app", "share", "ShareTargetPlugin.java")).read()
        self.assertIn('"office"', st)
        app = open(os.path.join(ROOT, "static", "js", "client", "app.js")).read()
        share = app[app.index("async function _consumeSendIntent(){"):]
        share = share[:share.index("const sig =")]
        self.assertRegex(share, r"target===\s*'office'\)\s*return false", "an Office share is attached to a post too")

    def test_the_page_opens_the_document_on_every_arrival_signal(self):
        app = open(os.path.join(ROOT, "static", "js", "client", "app.js")).read()
        self.assertRegex(app, r"const _reShare=\(\)=>\{[^\n]*_consumeOpenDoc\(\)", "nothing asks for the document")
