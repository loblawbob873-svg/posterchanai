"""webxdc mini apps: the zip reader and the attachment parser, as the CLIENT runs them.

    venv-unified/bin/python -m unittest tests.test_webxdc

Both halves are pure, and both fail in ways nothing on screen explains:

  * `zip.js` reads the `.xdc` container. A mis-read offset does not throw — it yields plausible bytes,
    and the app then fails to start with nothing to say whether the ARCHIVE was misread or the app is
    simply broken. So the archives here are built by Python's zipfile (a writer nobody involved
    controls) and read by the shipped parser under node, including the two shapes that are easy to
    get right by accident: a stored (uncompressed) entry, and an archive with a trailing comment,
    which moves the end-of-central-directory record off the end of the file.

  * `appOf` decides whether a post carries an app at all. Wrong, and either every post grows a Play
    button or none of them do — and the second one is silent.

The running half (the sandbox origin, the service worker, the bridge) is scripts/check_webxdc.py's
job: it needs two documents on two origins and a service worker, none of which exist under node.
"""
import io
import json
import shutil
import subprocess
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZIP_JS = ROOT / "static" / "js" / "client" / "zip.js"
WEBXDC_JS = ROOT / "static" / "js" / "client" / "webxdc.js"


def _node(script: str):
    out = subprocess.run(["node", "-e", script], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or "null")


def _xdc(files, comment=b"", compression=zipfile.ZIP_DEFLATED):
    """A real .xdc, built by a writer this repo does not control."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, data in files.items():
            z.writestr(name, data)
        if comment:
            z.comment = comment
    return buf.getvalue()


def _read(archive: bytes, script: str):
    """Run `script` under node with PCZip loaded and `BYTES` holding the archive."""
    boot = (
        "global.window = {};\n"
        f"const PCZip = require({json.dumps(str(ZIP_JS))});\n"
        f"const BYTES = Uint8Array.from({json.dumps(list(archive))});\n"
        "(async () => {\n" + script + "\n})().catch(e => { console.error(e); process.exit(1); });"
    )
    return _node(boot)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ZipReader(unittest.TestCase):
    def test_it_reads_a_deflated_app(self):
        # Compressible on purpose: a tiny string can end up STORED even in a deflate archive, which
        # would quietly test the wrong branch.
        body = "<!doctype html>" + ("<p>hello webxdc</p>" * 200)
        out = _read(_xdc({"index.html": body, "manifest.toml": 'name = "Chess"\n'}), """
            const files = await PCZip.readAll(BYTES);
            const dec = new TextDecoder();
            console.log(JSON.stringify({
              names: [...files.keys()].sort(),
              html: dec.decode(files.get('index.html')),
              manifest: dec.decode(files.get('manifest.toml')),
            }));
        """)
        self.assertEqual(out["names"], ["index.html", "manifest.toml"])
        self.assertEqual(out["html"], body)
        self.assertIn('name = "Chess"', out["manifest"])

    def test_it_reads_a_stored_entry(self):
        """Method 0 is not deflate with a flag — it is raw bytes, and inflating them fails."""
        out = _read(_xdc({"index.html": "<b>hi</b>", "a.png": "\x00\x01\x02"},
                         compression=zipfile.ZIP_STORED), """
            const files = await PCZip.readAll(BYTES);
            console.log(JSON.stringify({ html: new TextDecoder().decode(files.get('index.html')),
                                         png: [...files.get('a.png')] }));
        """)
        self.assertEqual(out["html"], "<b>hi</b>")
        self.assertEqual(out["png"], [0, 1, 2])

    def test_a_trailing_comment_does_not_hide_the_directory(self):
        """The end-of-central-directory record is last EXCEPT for a comment of up to 64KB, so its
        position is not fixed. Reading the last 22 bytes works until somebody's build tool adds one."""
        out = _read(_xdc({"index.html": "ok"}, comment=b"built by something" * 40), """
            const files = await PCZip.readAll(BYTES);
            console.log(JSON.stringify(new TextDecoder().decode(files.get('index.html'))));
        """)
        self.assertEqual(out, "ok")

    def test_nested_paths_survive_intact(self):
        out = _read(_xdc({"index.html": "x", "js/game.js": "run()", "img/s/p.png": "P"}), """
            const files = await PCZip.readAll(BYTES);
            console.log(JSON.stringify([...files.keys()].sort()));
        """)
        self.assertEqual(out, ["img/s/p.png", "index.html", "js/game.js"])

    def test_a_traversing_name_cannot_climb_out(self):
        """Entry names are attacker-controlled text and become the keys a sandboxed app fetches by."""
        out = _node("global.window={};const Z=require(%s);console.log(JSON.stringify("
                    "['../../etc/passwd','/abs/x.js','a/./b/../c.js','..\\\\..\\\\w.js']"
                    ".map(n=>Z.normalise(n))));" % json.dumps(str(ZIP_JS)))
        self.assertEqual(out, ["etc/passwd", "abs/x.js", "a/c.js", "w.js"])

    def test_something_that_is_not_a_zip_says_so(self):
        """"Not a zip" and "a zip with nothing in it" must not look the same to the UI."""
        out = _read(b"this is not a zip file at all, not even close", """
            try{ await PCZip.readAll(BYTES); console.log(JSON.stringify('no error')); }
            catch(e){ console.log(JSON.stringify(e.message)); }
        """)
        self.assertIn("not a zip", out)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class AttachmentParsing(unittest.TestCase):
    """Which posts carry a mini app. Both shapes from the NIP: an `imeta` tag on any event, and a
    kind-1063 file-metadata event, whose tags are flat instead."""

    def app_of(self, ev):
        boot = (
            "global.window = { addEventListener(){}, __PC: { $:()=>null, enc:s=>s, toast(){}, "
            "publish(){}, me:()=>null, profOf:()=>({}), apiBase:()=>'https://example.com' } };\n"
            "global.document = { addEventListener(){}, querySelectorAll:()=>[], createElement:()=>({"
            "  setAttribute(){}, classList:{add(){}}, appendChild(){}, style:{} }) };\n"
            "global.location = { hostname: 'example.com', href: 'https://example.com/' };\n"
            f"require({json.dumps(str(WEBXDC_JS))});\n"
            f"console.log(JSON.stringify(window.PCWebxdc.appOf({json.dumps(ev)})));"
        )
        return _node(boot)

    def test_an_imeta_attachment_is_found(self):
        ev = {"kind": 1, "content": "let's play", "tags": [
            ["imeta", "url https://blossom.example.com/abc.xdc", "m application/x-webxdc",
             "x " + "a" * 64, "webxdc 9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"]]}
        got = self.app_of(ev)
        self.assertEqual(got["url"], "https://blossom.example.com/abc.xdc")
        self.assertEqual(got["sha"], "a" * 64)
        self.assertEqual(got["uuid"], "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d")

    def test_a_kind_1063_file_event_is_found(self):
        ev = {"kind": 1063, "content": "A collaborative chess game.", "tags": [
            ["url", "https://blossom.example.com/abc.xdc"], ["m", "application/x-webxdc"],
            ["x", "b" * 64], ["alt", "Webxdc app: Chess"], ["webxdc", "u-u-i-d"]]}
        got = self.app_of(ev)
        self.assertEqual(got["url"], "https://blossom.example.com/abc.xdc")
        self.assertEqual(got["uuid"], "u-u-i-d")
        self.assertEqual(got["name"], "Chess", "the alt prefix should not become the app's name")

    def test_an_ordinary_post_carries_nothing(self):
        self.assertIsNone(self.app_of({"kind": 1, "content": "gm", "tags": []}))
        self.assertIsNone(self.app_of({"kind": 1, "content": "pic", "tags": [
            ["imeta", "url https://x.example/a.png", "m image/png"]]}))

    def test_a_non_http_url_is_refused(self):
        """The URL is fetched and its bytes are executed. `javascript:` and `file:` are not apps."""
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "/relative.xdc", ""):
            ev = {"kind": 1, "content": "", "tags": [
                ["imeta", "url " + bad, "m application/x-webxdc"]]}
            self.assertIsNone(self.app_of(ev), f"{bad!r} was accepted as an app")


if __name__ == "__main__":
    unittest.main()
