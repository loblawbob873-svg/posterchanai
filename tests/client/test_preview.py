"""Puts preview_sim.js into the suite, and pins the wiring the sim cannot see.

"basiucally i want the preview to app to handle videos, images when people click on them from
blossom". Blossom's only answer for a picture, a video or a PDF was "open in a new tab", which on
the encrypted drive means decrypting to a blob URL and handing it to the browser - you leave the
app and lose the folder you were in, and on the APK it does nothing useful at all.
"""
import os
import re
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "preview_sim.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@unittest.skipIf(not NODE, "no node on this node")
class PreviewSim(unittest.TestCase):
    def test_the_suite_passes(self):
        r = subprocess.run([NODE, SIM], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr)[-3000:])
        self.assertIn("preview holds", r.stdout)
        self.assertIn("it requests the maximised neutral document workspace", r.stdout)
        self.assertIn("destination reconstructs Preview from transferred bytes", r.stdout)

    def test_supplied_packaged_preview_runs_monitor_transfer(self):
        with tempfile.TemporaryDirectory() as td:
            packaged = os.path.join(td, "preview.js")
            shutil.copyfile(os.path.join(ROOT, "static/js/client/preview.js"), packaged)
            env = dict(os.environ, PC_INSTALLED_PREVIEW_JS=packaged)
            r = subprocess.run([NODE, SIM], env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, (r.stdout + r.stderr)[-3000:])
            self.assertIn("destination reconstructs Preview from transferred bytes", r.stdout)

    def test_monitor_handoff_routes_preview_state_before_generic_document_open(self):
        osjs = _read("static/js/client/os.js")
        payload = osjs[osjs.index("function handoffPayload"):osjs.index("function sendFrameHandoff")]
        receive = osjs[osjs.index("if(pcWM.onHandoffFrame)"):osjs.index("if(pcWM.onPreviewFrame)")]
        self.assertIn("typeof w.handoffState==='function'", payload)
        self.assertIn("PCPreview.acceptHandoff(p.state)", receive)
        self.assertLess(receive.index("PCPreview.acceptHandoff(p.state)"), receive.index("const w=reconstructHandoffWindow(p)"))
        send = osjs[osjs.index("function sendFrameHandoff"):osjs.index("function rearmFrameHandoffDestination")]
        self.assertIn("typeof w.handoffCancel==='function'", send)

    def test_failed_cross_renderer_blob_fetch_opens_recovery_document(self):
        preview = _read("static/js/client/preview.js")
        accept = preview[preview.index("async function acceptHandoff"):
                         preview.index("root.PCPreview =")]
        self.assertIn("preview bytes unavailable", accept)
        self.assertIn("Reopen the file from Files", accept)
        self.assertIn("return open({ name:String(s.name || 'Preview'), mime:'text/plain'", accept)

    def test_handoff_waits_for_metadata_and_video_remains_fitted(self):
        preview = _read("static/js/client/preview.js")
        restore = preview[preview.index("function restoreHandoffMedia"):
                          preview.index("function bytesOf")]
        self.assertIn("readyState", restore)
        self.assertIn("addEventListener('loadedmetadata', apply, { once:true })", restore)
        css = _read("static/css/client.css")
        rule = css[css.index(".pv-vid{"):css.index("}", css.index(".pv-vid{"))]
        self.assertIn("width:100%", rule)
        self.assertIn("height:100%", rule)
        self.assertIn("object-fit:contain", rule)


class PreviewIsReachable(unittest.TestCase):
    """A viewer nothing opens is a file in a directory."""

    @classmethod
    def setUpClass(cls):
        cls.app = _read("static/js/client/app.js")
        cls.tpl = _read("templates/client.html")
        cls.sw = _read("static/js/client/sw.js")
        cls.css = _read("static/css/client.css")

    def test_the_chooser_offers_it(self):
        i = self.app.index("function _handlersFor(")
        block = self.app[i:self.app.index("return out;", i)]
        self.assertIn("id:'preview'", block, "clicking a photograph offers no way to look at it")

    def test_pdf_preview_is_offered_on_a_cold_start(self):
        """The module is lazy. Its missing global must not make Preview disappear before first use."""
        i = self.app.index("const _previewable")
        block = self.app[i:self.app.index(";", i) + 1]
        self.assertNotIn("PCPreview", block)
        self.assertIn("_PREVIEW_EXT", block)
        rx = re.search(r"const _PREVIEW_EXT = (/[^\n]*?/i);", self.app)
        self.assertTrue(rx)
        self.assertRegex("manual.pdf", re.compile(rx.group(1)[1:-2], re.I))

    def test_it_is_offered_before_the_editors(self):
        """Looking at a file is the lightest thing you can do with it, and it is what somebody
        clicking a photograph meant. An editor first is a chooser that leads with the wrong answer."""
        i = self.app.index("function _handlersFor(")
        block = self.app[i:self.app.index("return out;", i)]
        self.assertLess(block.index("id:'preview'"), block.index("id:'code'"))
        self.assertLess(block.index("id:'preview'"), block.index("id:'office'"))

    def test_every_source_can_feed_it(self):
        """The drive (plain AND encrypted) and a synced folder. A viewer wired to one of the three
        is a button that works in one folder and not the next."""
        i = self.app.index("async function _previewBytes(")
        body = self.app[i:self.app.index("async function openPreviewFile(", i)]
        self.assertIn("_syncFileBlob", body, "a synced file cannot be previewed")
        self.assertIn("encFileUrl", body, "an encrypted file cannot be previewed")
        self.assertIn("d.url", body, "a plain drive file cannot be previewed")

    def test_lazy_viewer_failure_is_caught_in_the_file_open_transaction(self):
        """A missing packaged preview module must report an error, not leave a black/no-op screen."""
        i = self.app.index("async function openPreviewFile(")
        body = self.app[i:self.app.index("function _handlersFor(", i)]
        self.assertIn("const P = await _withModule('preview.js', 'PCPreview')", body)
        self.assertIn("if(!P || typeof P.open!=='function')", body)
        self.assertNotIn("_withModule('preview.js', 'PCPreview', P =>", body)
        self.assertLess(body.index("try{"), body.index("await _withModule"))
        self.assertLess(body.index("await _withModule"), body.index("catch(err)"))

    def test_media_click_opens_preview_directly_in_every_source(self):
        self.assertGreaterEqual(self.app.count("if(_previewable(_openFileName("), 2,
                                "Blossom and synced media must bypass the Open With chooser")
        start = self.app.index("openFile: async (path, name, openHere, mime)")
        host = self.app[start:self.app.index("toast, prompt: uiPrompt", start)]
        self.assertIn("if(_previewable(name || path, mime))", host)
        self.assertIn("window.pcHost.read(path, 256 * 1024 * 1024)", host)
        # The name is resolved into `nm` first, because the streamed branch needs it too.
        self.assertIn("P.open({ name:nm", host)

    def test_blossom_list_without_url_gets_a_canonical_blob_address(self):
        self.assertIn("url:b.url || (server.replace(/\\/$/,'') + '/' + b.sha256)", self.app)

    def test_the_module_is_shipped_and_cached(self):
        """It is lazily loaded through _withModule, so a missing script tag is survivable - but a
        missing PRECACHE entry means the viewer is simply absent offline, which is exactly when
        somebody is looking through their own drive."""
        self.assertIn("client/preview.js", self.tpl, "the module is never loaded")
        self.assertIn("'/static/js/client/preview.js'", self.sw, "not precached; absent offline")
        self.assertIn("'/static/vendor/pdfjs/pdf.min.js'", self.sw,
                      "the mobile PDF renderer is absent offline")
        self.assertIn("'/static/vendor/pdfjs/pdf.worker.min.js'", self.sw,
                      "PDF.js starts but its worker is absent offline")

    def test_service_worker_install_actually_precaches_both_pdf_renderer_parts(self):
        """Run the shipped worker install callback; list membership alone does not prove it is used."""
        script = r"""
const fs=require('fs'),vm=require('vm');
const handlers={},added=[];
global.self={location:{pathname:'/client/sw.js'},addEventListener:(k,f)=>handlers[k]=f,
  skipWaiting:()=>{},clients:{claim:()=>Promise.resolve()}};
global.caches={open:async()=>({add:async u=>{added.push(u)}}),keys:async()=>[]};
global.fetch=async()=>({ok:true,status:200,clone(){return this}});
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'),{filename:process.argv[1]});
let pending=Promise.resolve();handlers.install({waitUntil:p=>pending=p});
pending.then(()=>console.log(JSON.stringify(added))).catch(e=>{console.error(e);process.exit(1)});
"""
        got = json.loads(subprocess.check_output(
            ["node", "-e", script, str(Path(__file__).parents[2] / "static/js/client/sw.js")],
            text=True,
        ))
        self.assertIn('/static/vendor/pdfjs/pdf.min.js', got)
        self.assertIn('/static/vendor/pdfjs/pdf.worker.min.js', got)

    def test_the_back_button_closes_the_picture_not_the_folder(self):
        """It is a full-screen sheet on a phone, opened FROM a screen the person still wants."""
        i = self.app.index("PCPreview.isOpen")
        self.assertGreater(i, 0, "Android Back does not close the preview")
        seg = self.app[i - 400:i + 200]
        self.assertIn("PCPreview.close()", seg)

    def test_it_is_styled_for_both_hosts(self):
        """A desktop window's slot and a full-screen sheet. Without the rules the media is sized by
        the viewport inside a window somebody just resized - the office editor's "tiny white box"."""
        for sel in (".pv-host{", ".pv-win{", ".pv-sheet{", ".pv-vid{", ".pv-img{", ".pv-pdf-page{"):
            self.assertIn(sel, self.css, f"{sel} has no rule")

    def test_desktop_preview_uses_the_whole_neutral_document_window(self):
        src = _read("static/js/client/preview.js")
        osjs = _read("static/js/client/os.js")
        self.assertIn("root.PCOS.documentWindow(w)", src)
        self.assertIn("function documentWindow(w)", osjs)
        self.assertIn("w.el.classList.add('osw-document')", osjs)
        self.assertIn("snapTo(w, 'max')", osjs)
        self.assertIn(".osw.osw-document", self.css)
        self.assertIn(".osw-slot.office-win{opacity:1;overflow:hidden;padding:10px}", self.css)
        self.assertIn(".osw-slot.pv-win{opacity:1;overflow:hidden;padding:0}", self.css)
        self.assertIn(".pc-document-focus .scanlines", self.css)
        self.assertIn("classList.remove('pc-document-focus')", osjs)

    def test_minimising_the_last_document_restores_desktop_effects(self):
        """Document chrome is a focus state, not a sticky preference.

        With Preview as the only visible window, minimise has no successor to pass through
        ``focusWin``.  That branch must explicitly restore the desktop just like close does.
        """
        osjs = _read("static/js/client/os.js")
        start = osjs.index("function minimise(w)")
        body = osjs[start:osjs.index("function taskbarMove(w)", start)]
        no_successor = body[body.index("if(next) focusWin(next)"):]
        self.assertIn("classList.remove('pc-document-focus')", no_successor)
        self.assertLess(no_successor.index("classList.remove('pc-document-focus')"),
                        no_successor.index("drawBar()"))

    def test_android_pdfs_use_the_bundled_renderer(self):
        src = _read("static/js/client/preview.js")
        self.assertIn("/static/vendor/pdfjs/pdf.min.js", src)
        self.assertIn("pdf.worker.min.js", src)
        self.assertIn("getDocument({ data: await blob.arrayBuffer() })", src)
        self.assertNotIn("ships no PDF viewer", src)

    def test_pdf_engine_failure_is_bounded_retryable_and_has_a_native_fallback(self):
        """Android WebView has no built-in PDF renderer. A pdf.js failure must not remain a
        permanent spinner, poison later attempts, or leave the reader without a useful way out."""
        src = _read("static/js/client/preview.js")
        loader = src[src.index("function loadPdfJs()"):
                     src.index("function openElsewhere", src.index("function loadPdfJs()"))]
        self.assertIn("setTimeout", loader)
        self.assertIn("_pdfjs = null", loader)
        self.assertIn("PDF renderer timed out", loader)
        render = src[src.index("function renderPdf"):
                     src.index("var _open", src.index("function renderPdf"))]
        self.assertIn("pv-pdf-native", render)
        self.assertIn("openElsewhere(blob, name)", render)
        fallback = src[src.index("function openElsewhere"):
                       src.index("function renderPdf")]
        self.assertIn("saveBlobAs", fallback)
        self.assertIn("document.pdf", fallback)

    def test_closing_a_pdf_cancels_rendering_and_destroys_the_document(self):
        """A closed long PDF must not keep rendering pages and allocating canvases off-screen."""
        src = _read("static/js/client/preview.js")
        render = src[src.index("function renderPdf"):
                     src.index("var _open", src.index("function renderPdf"))]
        self.assertIn("if (task && task.cancel) task.cancel()", render)
        self.assertIn("if (pdf && pdf.destroy) pdf.destroy()", render)
        self.assertIn("if (stopped) return", render)
        mount = src[src.index("function mount("):src.index("function open(file)")]
        self.assertIn("cleanup = renderPdf(host, blob, name)", mount)
        self.assertIn("return cleanup", mount)
        close = src[src.index("var done = function"):
                    src.index("var onKey", src.index("var done = function"))]
        self.assertIn("mountCleanup()", close)

    def test_the_download_never_uses_a_bare_anchor(self):
        """The APK's WebView ignores a programmatic download and the desktop's app:// origin refuses
        one, so `<a download>` is a button that silently does nothing on two of three platforms."""
        src = _read("static/js/client/preview.js")
        self.assertIn("saveBlobAs", src)
        self.assertNotIn("download=", src)

    def test_the_blob_url_is_released(self):
        """One leaked object URL per file means every picture you looked at is still in memory."""
        src = _read("static/js/client/preview.js")
        self.assertIn("revokeObjectURL", src)

    def test_closing_stops_the_sound(self):
        """A <video> detached from the document keeps playing in Chromium until it is collected."""
        src = _read("static/js/client/preview.js")
        self.assertIn(".pause()", src, "a closed window would keep talking")

    def test_generic_blossom_video_is_given_a_real_media_type(self):
        """Old Blossom objects commonly say octet-stream; preserving that on the blob URL makes
        Chromium render a black video surface and refuse to start an otherwise valid MP4."""
        src = _read("static/js/client/preview.js")
        self.assertIn("application\\/(octet-stream|binary)", src)
        self.assertIn("mp4:'video/mp4'", src)
        self.assertIn("av.load()", src)
        self.assertIn("Loading video", src)

    def test_media_stall_has_a_bounded_download_recovery(self):
        src = _read("static/js/client/preview.js")
        self.assertIn("This video did not start on this device", src)
        self.assertIn("}, 12000)", src)
        self.assertIn("clearTimeout(stalled)", src)


if __name__ == "__main__":
    unittest.main()
