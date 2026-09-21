"""`pc-open` — "Need way in CLI to open files in office, PosterChan Code, PosterChan preview, etc".

Office, Code and Preview are windows inside the desktop process, so a terminal can only ASK the
running shell. Three pieces, each RUN here rather than read:

  * os/bin/pc-open (python, stdlib) — resolves paths against the caller's cwd, refuses missing files
    with the shell's own words, picks its app from its NAME (pc-office/pc-code/pc-preview/pc-files),
    and exits 0 / 1 (some file failed) / 2 (usage) / 3 (no desktop answering);
  * desktop/opener.js — the socket in $XDG_RUNTIME_DIR, the request parser, and a second check of
    every path before anything reaches the page. Driven END TO END below: the shipped listener under
    node, the shipped CLI against it;
  * app.js `_openFromCommandLine` — which app a file lands in. The shipped function is evaluated with
    the shipped `_previewable`/`_officeable` rules, so `auto` cannot drift from what Files does.
"""
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "os" / "bin" / "pc-open"
PKG = ROOT / "os" / "overlay" / "app-misc" / "posterchanos-shell"
OPENER = ROOT / "desktop" / "opener.js"
MAIN = ROOT / "desktop" / "main.js"
PRELOAD = ROOT / "desktop" / "preload.js"
APP = ROOT / "static" / "js" / "client" / "app.js"
NODE = shutil.which("node")


class FakeDesktop:
    """A socket that answers like the desktop and records what it was asked."""

    def __init__(self, path, answer):
        self.path, self.answer, self.requests = path, answer, []
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(path)
        self.sock.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            buf = b""
            while b"\n" not in buf:
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
            req = json.loads(buf.decode())
            self.requests.append(req)
            c.sendall((json.dumps(self.answer(req)) + "\n").encode())
            c.close()

    def close(self):
        self.sock.close()


def ok_all(req):
    names = {"auto": "Office", "office": "Office", "code": "Code", "preview": "Preview", "files": "Files"}
    return {"ok": True, "results": [{"path": p, "ok": True, "app": names[req["app"]]} for p in req["paths"]]}


class TheCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "work").mkdir()
        (self.tmp / "work" / "report.docx").write_bytes(b"PK")
        (self.tmp / "work" / "notes.txt").write_text("hi")
        (self.tmp / "work" / "sub").mkdir()
        self.sock = str(self.tmp / "open.sock")

    def run_cli(self, *args, name="pc-open", cwd=None):
        exe = self.tmp / name
        if not exe.exists():
            exe.symlink_to(CLI)
        env = dict(os.environ, PC_OPEN_SOCKET=self.sock, PC_OPEN_TIMEOUT="10")
        return subprocess.run([sys.executable, str(exe), *args], cwd=cwd or self.tmp / "work",
                              env=env, capture_output=True, text=True, timeout=30)

    def desktop(self, answer=ok_all):
        d = FakeDesktop(self.sock, answer)
        self.addCleanup(d.close)
        return d

    def test_relative_paths_are_resolved_against_the_callers_directory(self):
        d = self.desktop()
        r = self.run_cli("report.docx", "./notes.txt", "sub/../notes.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        w = str(self.tmp / "work")
        self.assertEqual(d.requests[0]["paths"], [w + "/report.docx", w + "/notes.txt", w + "/notes.txt"])
        self.assertEqual(d.requests[0]["app"], "auto")
        self.assertIn("opened report.docx in Office", r.stdout)

    def test_the_name_picks_the_app(self):
        for name, app in (("pc-office", "office"), ("pc-code", "code"), ("pc-preview", "preview"),
                          ("pc-files", "files"), ("pc-open", "auto")):
            with self.subTest(name=name):
                d = self.desktop()
                r = self.run_cli("notes.txt", name=name)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(d.requests[-1]["app"], app)
                d.close(); os.unlink(self.sock)

    def test_a_flag_overrides_and_flags_are_exclusive(self):
        d = self.desktop()
        r = self.run_cli("--code", "report.docx", name="pc-office")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(d.requests[0]["app"], "code")
        r = self.run_cli("--code", "--office", "report.docx")
        self.assertEqual(r.returncode, 2)

    def test_a_missing_file_is_named_and_not_sent(self):
        d = self.desktop()
        r = self.run_cli("nope.odt", "notes.txt")
        self.assertEqual(r.returncode, 1)
        self.assertIn("pc-open: nope.odt: No such file or directory", r.stderr)
        self.assertEqual(len(d.requests[0]["paths"]), 1, "the missing file reached the desktop")
        self.assertIn("opened notes.txt", r.stdout, "one bad name must not cost the good one")

    def test_only_missing_files_never_connects(self):
        d = self.desktop()
        r = self.run_cli("gone.txt")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(d.requests, [])

    def test_a_folder_needs_files(self):
        d = self.desktop()
        r = self.run_cli("--code", "sub")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Is a directory (use --files)", r.stderr)
        r = self.run_cli("--files", "sub")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_desktop_is_exit_3_with_the_socket_named(self):
        r = self.run_cli("notes.txt")
        self.assertEqual(r.returncode, 3)
        self.assertIn("not answering", r.stderr)
        self.assertIn(self.sock, r.stderr)

    def test_a_desktop_that_is_still_starting_is_exit_3(self):
        self.desktop(lambda req: {"ok": False, "why": "the desktop is still starting — try again in a moment"})
        r = self.run_cli("notes.txt")
        self.assertEqual(r.returncode, 3)
        self.assertIn("still starting", r.stderr)

    def test_the_desktops_refusal_is_reported_per_file(self):
        def answer(req):
            return {"ok": False, "results": [
                {"path": req["paths"][0], "ok": False, "why": "PosterChan Office does not open this kind of file"},
                {"path": req["paths"][1], "ok": True, "app": "Office"}]}
        self.desktop(answer)
        r = self.run_cli("--office", "notes.txt", "report.docx")
        self.assertEqual(r.returncode, 1)
        self.assertIn("pc-open: notes.txt: PosterChan Office does not open this kind of file", r.stderr)
        self.assertIn("opened report.docx in Office", r.stdout)

    def test_help_explains_the_exit_codes(self):
        r = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        for w in ("--office", "--code", "--preview", "--files", "3 the desktop is not running"):
            self.assertIn(w, r.stdout)

    def test_the_default_socket_is_in_the_runtime_directory(self):
        sys.path.insert(0, str(ROOT / "os" / "bin"))
        try:
            import importlib.machinery, importlib.util
            loader = importlib.machinery.SourceFileLoader("pc_open", str(CLI))
            spec = importlib.util.spec_from_loader("pc_open", loader)
            mod = importlib.util.module_from_spec(spec); loader.exec_module(mod)
        finally:
            sys.path.pop(0)
        self.assertEqual(mod.socket_path(env={"XDG_RUNTIME_DIR": "/run/user/5"}),
                         "/run/user/5/posterchan-open.sock")
        self.assertEqual(mod.socket_path(env={"PC_OPEN_SOCKET": "/x"}), "/x")


@unittest.skipIf(not NODE, "node is unavailable")
class TheListener(unittest.TestCase):
    """desktop/opener.js under node — and the real CLI talking to it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def node(self, js, *args, timeout=20):
        r = subprocess.run([NODE, "-e", js, str(OPENER), *map(str, args)],
                           capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_requests_are_parsed_strictly(self):
        js = r"""
const o = require(process.argv[1]);
const t = (s) => { try { return JSON.stringify(o.parseRequest(s)); } catch (e) { return 'ERR ' + e.message; } };
for (const s of ['nope', '{"v":2,"paths":["/a"]}', '{"v":1,"app":"shell","paths":["/a"]}',
                 '{"v":1,"paths":[]}', '{"v":1,"paths":["rel/a"]}', '{"v":1,"paths":["/a/../b"]}'])
  console.log(t(s));
"""
        out = self.node(js).splitlines()
        self.assertTrue(out[0].startswith("ERR"))
        self.assertIn("version", out[1])
        self.assertIn("unknown app", out[2])
        self.assertIn("no files", out[3])
        self.assertIn("not an absolute path", out[4])
        self.assertEqual(json.loads(out[5]), {"app": "auto", "paths": ["/b"]})

    def test_every_path_is_checked_before_the_page_sees_it(self):
        (self.tmp / "a.txt").write_text("x")
        (self.tmp / "d").mkdir()
        os.mkfifo(self.tmp / "fifo")
        js = r"""
const o = require(process.argv[1]); const d = process.argv[2];
const seen = [];
const deliver = async (app, items) => { seen.push(...items.map(i => i.path)); return items.map(i => ({path:i.path, ok:true, app:'Code'})); };
o.handleWith(deliver, {app:'code', paths:[d+'/a.txt', d+'/missing', d+'/d', d+'/fifo']}).then(r =>
  console.log(JSON.stringify({r, seen})));
"""
        got = json.loads(self.node(js, self.tmp))
        res = {Path(x["path"]).name: x for x in got["r"]["results"]}
        self.assertEqual([Path(p).name for p in got["seen"]], ["a.txt"])
        self.assertTrue(res["a.txt"]["ok"])
        self.assertEqual(res["missing"]["why"], "No such file or directory")
        self.assertIn("Is a directory", res["d"]["why"])
        self.assertEqual(res["fifo"]["why"], "Not a regular file")
        self.assertFalse(got["r"]["ok"])

    def test_the_real_cli_against_the_real_listener(self):
        """End to end, exactly the shapes that cross the socket in production."""
        sock = self.tmp / "open.sock"
        doc = self.tmp / "report.docx"; doc.write_bytes(b"PK")
        ready = self.tmp / "ready"
        js = r"""
const fs = require('fs'); const o = require(process.argv[1]);
const deliver = async (app, items) => items.map(i => ({path:i.path, ok:true, app:app === 'auto' ? 'Office' : app, kind:i.kind}));
const srv = o.listen(process.argv[2], (req) => o.handleWith(deliver, req), {
  onListening: () => { fs.writeFileSync(process.argv[3], String(fs.statSync(process.argv[2]).mode & 0o777)); } });
setTimeout(() => srv.close(), 8000);
"""
        proc = subprocess.Popen([NODE, "-e", js, str(OPENER), str(sock), str(ready)])
        self.addCleanup(proc.kill)
        for _ in range(100):
            if ready.exists() and ready.read_text():
                break
            time.sleep(0.05)
        self.assertEqual(int(ready.read_text()), 0o600, "anyone on the machine could ask this desktop")
        env = dict(os.environ, PC_OPEN_SOCKET=str(sock))
        r = subprocess.run([sys.executable, str(CLI), "report.docx", "ghost.odt"], cwd=self.tmp, env=env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("opened report.docx in Office", r.stdout)
        self.assertIn("ghost.odt: No such file or directory", r.stderr)

    def test_a_non_socket_in_the_way_is_refused_not_deleted(self):
        p = self.tmp / "open.sock"; p.write_text("somebody's file")
        js = r"""
const o = require(process.argv[1]);
try { o.listen(process.argv[2], () => ({})); console.log('LISTENED'); } catch (e) { console.log('REFUSED'); }
"""
        self.assertIn("REFUSED", self.node(js, p))
        self.assertEqual(p.read_text(), "somebody's file")


@unittest.skipIf(not NODE, "node is unavailable")
class WhereAFileLands(unittest.TestCase):
    """The SHIPPED `_openFromCommandLine`, with the SHIPPED `_previewable`/`_officeable` rules."""

    @classmethod
    def setUpClass(cls):
        app = APP.read_text(encoding="utf-8")
        def grab(start, end):
            i = app.index(start)
            return app[i:app.index(end, i)]
        cls.rules = "\n".join([
            grab("  const _OFFICE_EXT =", "\n  /* WHAT POSTERCHAN CODE WILL OPEN"),
            grab("  const _PREVIEW_EXT =", "\n  /* Blossom implementations"),
        ])
        cls.fn = grab("  const _PC_OPEN_APPS =", "\n  if(window.pcHost && typeof pcHost.onOpenRequest")

    def route(self, app, items, fail=None):
        js = """
const calls = [];
const window = { pcHost: { read(){} } }; const pcHost = window.pcHost;
const mimeForName = () => '';
const FAIL = %s;
const stub = (n) => async (...a) => { calls.push([n, a[0]]); if (FAIL === n) throw new Error(n + ' broke'); return true; };
const _hostOpenPreview = stub('preview'), _hostOpenOffice = stub('office'), _hostOpenCode = stub('code');
const _hostOpenFolder = (d) => { calls.push(['files', d]); return true; };
%s
%s
_openFromCommandLine(%s).then(out => console.log(JSON.stringify({out, calls})));
""" % (json.dumps(fail), self.rules, self.fn, json.dumps({"app": app, "items": items}))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_auto_picks_what_files_would(self):
        got = self.route("auto", [{"path": "/h/a.jpg", "kind": "file"}, {"path": "/h/b.docx", "kind": "file"},
                                  {"path": "/h/c.py", "kind": "file"}, {"path": "/h/d.pdf", "kind": "file"},
                                  {"path": "/h/Music", "kind": "dir"}])
        self.assertEqual(got["calls"], [["preview", "/h/a.jpg"], ["office", "/h/b.docx"], ["code", "/h/c.py"],
                                        ["preview", "/h/d.pdf"], ["files", "/h/Music"]])
        self.assertEqual([o["app"] for o in got["out"]], ["Preview", "Office", "Code", "Preview", "Files"])
        self.assertTrue(all(o["ok"] for o in got["out"]))

    def test_an_explicit_app_is_obeyed_even_over_preview(self):
        got = self.route("office", [{"path": "/h/d.pdf", "kind": "file"}])
        self.assertEqual(got["calls"], [["office", "/h/d.pdf"]])
        got = self.route("code", [{"path": "/h/logo.svg", "kind": "file"}])
        self.assertEqual(got["calls"], [["code", "/h/logo.svg"]])

    def test_an_app_that_cannot_take_the_file_says_so_instead_of_substituting(self):
        got = self.route("office", [{"path": "/h/notes.txt", "kind": "file"}])
        self.assertEqual(got["calls"], [])
        self.assertFalse(got["out"][0]["ok"])
        self.assertIn("Office does not open", got["out"][0]["why"])
        got = self.route("preview", [{"path": "/h/b.docx", "kind": "file"}])
        self.assertFalse(got["out"][0]["ok"])

    def test_files_opens_the_files_folder(self):
        got = self.route("files", [{"path": "/h/docs/b.docx", "kind": "file"}])
        self.assertEqual(got["calls"], [["files", "/h/docs"]])

    def test_a_failure_is_an_answer_not_a_throw(self):
        got = self.route("auto", [{"path": "/h/b.docx", "kind": "file"}, {"path": "/h/c.py", "kind": "file"}],
                         fail="office")
        self.assertEqual(got["out"][0], {"path": "/h/b.docx", "ok": False, "app": "Office", "why": "office broke"})
        self.assertTrue(got["out"][1]["ok"], "one failure must not cost the next file")


class TheWiring(unittest.TestCase):
    def test_the_main_process_listens_only_as_the_os_shell(self):
        main = MAIN.read_text(encoding="utf-8")
        fn = main[main.index("function wireOpener() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("!SHELL_MODE", fn)
        self.assertIn("diagnostic", fn)
        self.assertIn("XDG_RUNTIME_DIR", fn)
        self.assertIn("opener.SOCKET_NAME", fn)
        self.assertRegex(main, r"wireShellRecovery\(\);\n\s+wireOpener\(\);")
        deliver = main[main.index("function deliverOpen(app, items) {"):]
        deliver = deliver[:deliver.index("\n}\n")]
        self.assertIn("_openReady.has(", deliver, "a request before the page can answer must be refused, not hang")
        self.assertIn("still starting", deliver)

    def test_the_page_answers_through_preload(self):
        pre = PRELOAD.read_text(encoding="utf-8")
        self.assertIn("onOpenRequest: (fn) =>", pre)
        self.assertIn("ipcRenderer.send('pc:host:open-ready')", pre)
        self.assertIn("ipcRenderer.send('pc:host:open-result'", pre)
        app = APP.read_text(encoding="utf-8")
        self.assertIn("pcHost.onOpenRequest(_openFromCommandLine)", app)

    def test_the_chooser_and_the_command_line_share_one_opener_per_app(self):
        app = APP.read_text(encoding="utf-8")
        chooser = app[app.index("openFile: async (path, name, openHere, mime) => {"):
                      app.index("toast, prompt: uiPrompt, confirm: uiConfirm,")]
        for helper in ("_hostOpenPreview(", "_hostOpenOffice(", "_hostOpenCode("):
            self.assertIn(helper, chooser)
        self.assertEqual(app.count("async function _hostOpenOffice("), 1)
        self.assertNotIn("_officeSession(", chooser, "a second copy of the Office opener")


class ThePackage(unittest.TestCase):
    def ebuild(self):
        eb = [f for f in os.listdir(PKG) if f.endswith(".ebuild")][0]
        return (PKG / eb).read_text(encoding="utf-8")

    def test_it_is_installed_with_its_aliases(self):
        eb = self.ebuild()
        helpers = re.search(r"for helper in ([^;]+); do", eb).group(1).split()
        self.assertIn("pc-open", helpers)
        self.assertEqual((PKG / "files" / "pc-open").read_bytes(), CLI.read_bytes())
        self.assertTrue(os.access(CLI, os.X_OK))
        for alias in ("pc-office", "pc-code", "pc-preview", "pc-files"):
            self.assertIn(alias, eb)
        self.assertIn('dosym pc-open "/usr/local/bin/${alias}"', eb)

    def test_the_direct_installer_copies_it_too(self):
        sh = (ROOT / "os" / "gentoo.sh").read_text(encoding="utf-8")
        loop = [l for l in sh.splitlines() if "for helper in" in l and "pc-idle" in l][0]
        self.assertIn("pc-open", loop.split())
        self.assertIn('ln -sf pc-open "${TARGET}/usr/local/bin/$alias"', sh)

    def test_xdg_open_reaches_it_for_office_documents_and_nothing_else(self):
        desktop = (PKG / "files" / "posterchan-open.desktop").read_text(encoding="utf-8")
        self.assertIn("Exec=/usr/local/bin/pc-open %F", desktop)
        self.assertIn("NoDisplay=true", desktop, "a file handler must not appear in the start menu")
        types = [t for t in re.search(r"^MimeType=(.*)$", desktop, re.M).group(1).split(";") if t]
        self.assertIn("application/vnd.openxmlformats-officedocument.wordprocessingml.document", types)
        self.assertIn("application/vnd.oasis.opendocument.text", types)
        for taken in ("application/pdf", "text/plain", "text/csv"):
            self.assertNotIn(taken, types, taken + " already has a handler on this machine")
        self.assertFalse([t for t in types if t.startswith(("image/", "video/", "audio/"))])
        mime = (PKG / "files" / "posterchanos-mimeapps.list").read_text(encoding="utf-8")
        body = mime.split("[Default Applications]\n", 1)[1]
        defaults = dict(l.split("=", 1) for l in body.splitlines() if l and not l.startswith("#"))
        self.assertEqual(sorted(defaults), sorted(types), "the defaults and the .desktop disagree")
        self.assertEqual(set(defaults.values()), {"posterchan-open.desktop"})
        eb = self.ebuild()
        self.assertIn('doins "${FILESDIR}/posterchan-open.desktop"', eb)
        self.assertIn('newins "${FILESDIR}/posterchanos-mimeapps.list" mimeapps.list', eb)

    def test_every_office_type_it_claims_is_one_office_opens(self):
        app = APP.read_text(encoding="utf-8")
        exts = re.search(r"const _OFFICE_EXT = /\\\.\(([^)]+)\)\$/i;", app).group(1).split("|")
        claims = {
            "msword": "doc", "wordprocessingml.document": "docx", "opendocument.text": "odt", "rtf": "rtf",
            "vnd.ms-excel": "xls", "spreadsheetml.sheet": "xlsx", "macroEnabled.12": "xlsm",
            "opendocument.spreadsheet": "ods", "ms-powerpoint": "ppt", "presentationml.presentation": "pptx",
            "opendocument.presentation": "odp", "opendocument.graphics": "odg",
            "text-template": "ott", "spreadsheet-template": "ots", "presentation-template": "otp",
            "graphics-template": "otg", "text-flat-xml": "fodt", "spreadsheet-flat-xml": "fods",
            "presentation-flat-xml": "fodp", "graphics-flat-xml": "fodg", "sun.xml.writer": "sxw",
            "sun.xml.calc": "sxc", "sun.xml.impress": "sxi"}
        desktop = (PKG / "files" / "posterchan-open.desktop").read_text(encoding="utf-8")
        types = [t for t in re.search(r"^MimeType=(.*)$", desktop, re.M).group(1).split(";") if t]
        for t in types:
            ext = next((e for k, e in sorted(claims.items(), key=lambda kv: -len(kv[0])) if t.endswith(k)), None)
            self.assertIsNotNone(ext, t)
            self.assertIn(ext, exts, "%s is claimed but Office does not open .%s" % (t, ext))


if __name__ == "__main__":
    unittest.main()
