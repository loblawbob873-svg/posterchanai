"""A LOCAL VIDEO IS STREAMED FROM AN ADDRESS, NOT READ INTO THE RENDERER.

Opening a file on This Computer went through `pc:host:read`, which does a SYNCHRONOUS readFileSync
of the whole file in the desktop's main process and ships the bytes through IPC to be made into a
Blob. For a picture or a PDF that is fine. For media it is wrong three times over:

  * the main process is blocked for the length of the read, so the whole desktop stops;
  * a Blob URL cannot be range-requested, so the player can neither seek nor start before the last
    byte -- a black frame with controls that control nothing;
  * anything past the bridge's ceiling is refused outright.

Reported as "playing video in blossom ... is black, buttons hidden" and "can't play .webm in file
manager", both on This Computer.

`app://posterchan/__hostfile/<path>` serves the file with `Accept-Ranges`. It grants nothing new:
the path goes through the SAME `hostfs().clean()` gate the read bridge uses, so exactly the files a
renderer could already read are the ones it can now address.

These tests RUN the handler against real files, because the bug is in what the response says.
"""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
PREVIEW = (ROOT / "static/js/client/preview.js").read_text(encoding="utf-8")


def run_handler(script):
    """Lift serveHostFile out of main.js and run it against a stubbed hostfs + real files."""
    body = MAIN[MAIN.index("function serveHostFile(request, rel) {"):]
    body = body[: body.index("\nfunction serveBundle()")]
    tmp = Path(tempfile.mkdtemp())
    try:
        js = tmp / "t.mjs"
        js.write_text(
            "import fs from 'node:fs';\nimport path from 'node:path';\n"
            "const _MIME = {'.webm':'video/webm','.mp4':'video/mp4'};\n"
            "let ALLOW = '';\n"
            "const hostfs = () => ({ clean: (p) => (p && p.startsWith(ALLOW) ? p : '') });\n"
            + body + "\n" + script)
        out = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=120,
                             cwd=str(tmp))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


HARNESS = """
const dir = fs.mkdtempSync('/tmp/pc-hostfile-');
ALLOW = dir;
const file = path.join(dir, 'clip.webm');
const bytes = Buffer.alloc(1000);
for (let i = 0; i < bytes.length; i++) bytes[i] = i & 0xff;
fs.writeFileSync(file, bytes);
const req = (range) => ({ headers: { get: (k) => (k.toLowerCase() === 'range' ? (range || '') : '') } });
const rel = '/__hostfile/' + file;
async function body(res){ return Buffer.from(await res.arrayBuffer()); }
"""


class TestItStreamsWithRanges(unittest.TestCase):
    def _run(self, script):
        out = run_handler(HARNESS + script)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_a_whole_file_is_served_with_its_type_and_ranges(self):
        got = self._run("""
const res = serveHostFile(req(''), rel);
body(res).then(b => { console.log(JSON.stringify({
  status: res.status, type: res.headers.get('Content-Type'),
  ranges: res.headers.get('Accept-Ranges'), len: b.length, first: b[0], last: b[b.length-1] })); });
""")
        self.assertEqual(got["status"], 200)
        self.assertEqual(got["type"], "video/webm")
        self.assertEqual(got["ranges"], "bytes",
                         "without Accept-Ranges a player cannot seek and will not try")
        self.assertEqual(got["len"], 1000)

    def test_a_range_returns_exactly_that_slice(self):
        got = self._run("""
const res = serveHostFile(req('bytes=10-19'), rel);
body(res).then(b => { console.log(JSON.stringify({
  status: res.status, cr: res.headers.get('Content-Range'),
  len: b.length, first: b[0], last: b[b.length-1] })); });
""")
        self.assertEqual(got["status"], 206)
        self.assertEqual(got["cr"], "bytes 10-19/1000")
        self.assertEqual(got["len"], 10)
        self.assertEqual(got["first"], 10)
        self.assertEqual(got["last"], 19)

    def test_an_open_ended_range_runs_to_the_end(self):
        """`bytes=500-` is what a player sends when somebody drags the scrubber."""
        got = self._run("""
const res = serveHostFile(req('bytes=500-'), rel);
body(res).then(b => console.log(JSON.stringify({
  status: res.status, cr: res.headers.get('Content-Range'), len: b.length, first: b[0] })));
""")
        self.assertEqual(got["status"], 206)
        self.assertEqual(got["cr"], "bytes 500-999/1000")
        self.assertEqual(got["len"], 500)
        self.assertEqual(got["first"], 500 & 0xFF)

    def test_a_suffix_range_returns_the_tail(self):
        """`bytes=-64` is how a player finds a container's index at the end of the file."""
        got = self._run("""
const res = serveHostFile(req('bytes=-64'), rel);
body(res).then(b => console.log(JSON.stringify({
  status: res.status, cr: res.headers.get('Content-Range'), len: b.length })));
""")
        self.assertEqual(got["status"], 206)
        self.assertEqual(got["cr"], "bytes 936-999/1000")
        self.assertEqual(got["len"], 64)

    def test_a_range_past_the_end_is_refused_properly(self):
        got = self._run("""
const res = serveHostFile(req('bytes=5000-6000'), rel);
console.log(JSON.stringify({status: res.status, cr: res.headers.get('Content-Range')}));
""")
        self.assertEqual(got["status"], 416)
        self.assertEqual(got["cr"], "bytes */1000")


class TestItGrantsNothingNew(unittest.TestCase):
    def test_a_path_the_gate_refuses_is_forbidden(self):
        out = run_handler(HARNESS + """
const res = serveHostFile(req(''), '/__hostfile//etc/shadow');
console.log(JSON.stringify({status: res.status}));
""")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["status"], 403,
                         "a path the read bridge would refuse is addressable over the URL")

    def test_it_uses_the_same_gate_as_the_read_bridge(self):
        fn = MAIN[MAIN.index("function serveHostFile"):]
        fn = fn[: fn.index("\nfunction serveBundle")]
        self.assertIn("hostfs().clean(", fn,
                      "the URL handler applies its own containment rules instead of the bridge's")

    def test_it_does_not_decode_the_path_a_second_time(self):
        """The caller already decoded the pathname; decoding again is how an encoded traversal gets
        past a check that ran before it."""
        fn = MAIN[MAIN.index("function serveHostFile"):]
        fn = fn[: fn.index("\nfunction serveBundle")]
        self.assertNotIn("decodeURIComponent", fn)

    def test_a_missing_file_is_not_an_empty_success(self):
        """A player given an empty body reports a corrupt file and sends somebody looking at their
        video instead of at the path."""
        out = run_handler(HARNESS + """
const res = serveHostFile(req(''), '/__hostfile/' + path.join(dir, 'nope.webm'));
console.log(JSON.stringify({status: res.status}));
""")
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["status"], 404)


class TestTheCallersUseIt(unittest.TestCase):
    def test_preload_exposes_an_address_builder(self):
        self.assertIn("fileUrl:", PRELOAD)
        self.assertIn("__hostfile", PRELOAD)

    def test_the_client_streams_media_and_still_reads_everything_else(self):
        body = APP[APP.index("openFile: async (path, name, openHere, mime) => {"):]
        body = body[: body.index("_openWithSheet(")]
        self.assertIn("pcHost.fileUrl", body, "media is still read whole into the renderer")
        self.assertIn("pcHost.read", body,
                      "the byte path is gone, so a picture or a PDF has no way to open")

    def test_preview_accepts_a_url_for_media_only(self):
        body = PREVIEW[PREVIEW.index("  function open(file) {"):]
        body = body[: body.index("var key = 'pv:'")]
        self.assertIn("file.url", body)
        self.assertIn("isVideo(name, mime) || isAudio(name, mime)", body,
                      "a PDF or image would be handed a custom-scheme URL to an <iframe>")


if __name__ == "__main__":
    unittest.main()
