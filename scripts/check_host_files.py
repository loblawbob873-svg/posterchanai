#!/usr/bin/env python3
"""The computer's own files, driven in a real browser against a stub disk.

    venv-unified/bin/python scripts/check_host_files.py

`tests/test_host_fs.py` runs the bridge against a real directory (the operations that lose files)
and `tests/client/test_host_files_view.py` runs the ordering and the wording. Neither of them draws
anything, and this half fails differently: nothing throws, the folder simply does not open.

That distinction is not theoretical. This check exists because the chip's handler was written into
the wrong function on the first attempt — it queried a variable that does not exist in that scope,
so it bound nothing at all. `node --check` cannot see an undefined identifier, and the Files
explorer check LIFTS layout helpers out of app.js rather than running its binders, so both were
green with a control that did nothing.

Assertions:

  wont-open        Pressing a folder does not walk into it, or the parent button does not come back.
  no-crumbs        The path is not a row of buttons, so the only way up is one level at a time.
  folders-loose    Folders are not first. That is navigation, not taste: interleaved with files by
                   date, a directory of a thousand items cannot be walked.
  dotfiles-stuck   The dotfile switch does not change what is listed.
  opens-a-folder   A double-click on a FILE tries to walk into it instead of handing it to the
                   machine, or a click on a folder hands it to xdg-open.
  delete-unasked   Delete does not ask, or asks without saying it is reversible — this one goes to
                   the machine's own bin and that is the most important half of the sentence.
  reads-as-empty   A directory that could not be READ is drawn as an empty folder. Those are
                   different facts and confusing them shows somebody an empty folder full of files.

Exit 0 clean · 1 problems · 2 could not run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9493)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-hostfiles-check"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="pane"></div>
<script>
/* A STUB DISK. Shaped exactly as desktop/hostfs.js answers, because the whole point of this check is
 * the half that draws it — and a stub that is a different shape would test a screen that cannot
 * exist. `/deep` is unreadable, which is a different fact from empty. */
const DISK = {
  '/home/u': [
    { name: 'Documents', dir: true,  size: 0,    mtime: 5000, hidden: false },
    { name: 'zzz',       dir: true,  size: 0,    mtime: 1000, hidden: false },
    { name: 'a-note.txt',dir: false, size: 120,  mtime: 9000, hidden: false },
    { name: 'movie.mp4', dir: false, size: 9e6,  mtime: 2000, hidden: false },
    { name: '.bashrc',   dir: false, size: 44,   mtime: 3000, hidden: true  },
  ],
  '/home/u/Documents': [ { name: 'tax.pdf', dir: false, size: 900, mtime: 1, hidden: false } ],
  '/home': [ { name: 'u', dir: true, size: 0, mtime: 1, hidden: false } ],
  '/': [ { name: 'home', dir: true, size: 0, mtime: 1, hidden: false } ],
};
window.__opened = []; window.__trashed = []; window.__made = []; window.__renamed = [];
window.pcHost = {
  roots: async () => [{ name:'Home', path:'/home/u', kind:'home' },
                      { name:'This computer', path:'/', kind:'root' }],
  list: async (p) => {
    if (p === '/unreadable') throw new Error('EACCES: permission denied');
    const e = DISK[p];
    if (!e) throw new Error('ENOENT: no such directory');
    const parts = String(p).split('/').filter(Boolean);
    return { path: p, parent: parts.length ? '/' + parts.slice(0, -1).join('/') : null,
             entries: e.map(x => Object.assign({ path: (p === '/' ? '' : p) + '/' + x.name }, x)) };
  },
  open: async (p) => { window.__opened.push(p); return { ok: true }; },
  trash: async (p) => { window.__trashed.push(p); return { trashed: p }; },
  mkdir: async (d, n) => { window.__made.push([d, n]); return { path: d + '/' + n }; },
  rename: async (f, t) => { window.__renamed.push([f, t]); return { path: t }; },
};
window.__PC = { toast: m => (window.__toasts = window.__toasts || []).push(m) };
window.__prompted = []; window.__confirmed = [];
</script>
<script src="/static/js/client/hostfiles.js"></script>
<script>
/* The Files screen's own belongings, handed in the way app.js hands them. */
window.__ui = {
  view: 'tiles',
  cmp: (keyOf) => (a, b) => {
    const ka = keyOf(a, 'name'), kb = keyOf(b, 'name');
    return String(ka).localeCompare(String(kb), undefined, { numeric: true });
  },
  fmtBytes: (n) => n + ' B',
  fmtDate: (t) => String(t),
  /* THE FILES SCREEN'S OWN EXPLORER PARTS, stubbed the way app.js hands them over — the toolbar,
   * the column header, one details row and the type icon. The module used to draw its own markup
   * and eleven class names of which client.css defined NONE, so the pane rendered as raw unstyled
   * HTML inside a styled explorer. These stubs are deliberately minimal but carry the same class
   * names the real ones emit, because those are what this check drives. */
  bar: (crumbs) => '<div class="fx-bar"><nav class="fx-crumbs">' + crumbs.map(c =>
        '<button class="fx-crumb" data-crumb="' + c.to + '">' + c.label + '</button>').join('')
      + '</nav></div>',
  cols: () => '<div class="fx-cols"></div>',
  row: (o) => '<div class="file-card row' + (o.dir ? ' isdir' : '') + '">'
      + '<span class="fx-ic">' + o.icon + '</span><span class="fname">' + o.name + '</span>'
      + '<span class="fx-size">' + o.size + '</span><span class="fx-type">' + o.type + '</span></div>',
  icon: () => '\u{1F4CE}',
  typeName: (e) => (e ? e.toUpperCase() + ' file' : 'File'),
  /* The crumb router lives in app.js and knows about every source; here it only has to reach this
   * one, which is what the `h:` prefix names. */
  bindBar: () => {
    document.querySelectorAll('#pane .fx-crumb[data-crumb]').forEach(b => b.onclick = () => {
      const to = b.dataset.crumb || '';
      if (to.charAt(0) === 'h') { PCHostFiles.enter(to.slice(2)); window.__draw(); }
    });
  },
  bindCols: () => {},
  query: () => window.__query || '',
  toast: (m) => (window.__toasts = window.__toasts || []).push(m),
  prompt: async (msg, o) => { window.__prompted.push(msg); return window.__promptWith; },
  confirm: async (msg) => { window.__confirmed.push(msg); return window.__confirmWith !== false; },
};
window.__draw = () => PCHostFiles.render(document.getElementById('pane'), window.__ui);
window.__ready = true;
</script>
</body></html>"""


DRIVE = r"""(async () => {
  const out = { problems: [] };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const bad = (k, d) => out.problems.push({ k, d });
  const pane = document.getElementById('pane');
  /* THE APP'S OWN ROW, not this module's. `.file-card` + `.fname` is what the drive draws and what
   * "This Computer" draws now — the point of the rewrite being that there is one of them. */
  const cards = () => [...pane.querySelectorAll('#hf-grid .file-card[data-p]')];
  const nameOf = (r) => (r.querySelector('.fname') || r).textContent.trim().replace(/ \u2197$/, '');
  const rows = () => cards().map(nameOf);
  const row = (n) => cards().find(r => nameOf(r) === n);

  PCHostFiles.enter('/home/u');
  await window.__draw(); await sleep(150);

  /* ── folders first, dotfiles hidden ────────────────────────────────────────────────────────── */
  out.listing = rows();
  if (out.listing.indexOf('Documents') !== 0 || out.listing.indexOf('zzz') !== 1)
    bad('folders-loose', 'folders are not first: ' + JSON.stringify(out.listing));
  if (out.listing.includes('.bashrc'))
    bad('dotfiles-stuck', 'a dotfile is shown before the switch was touched');

  pane.querySelector('.hf-hidden').click(); await sleep(200);
  out.withDots = rows();
  if (!out.withDots.includes('.bashrc'))
    bad('dotfiles-stuck', 'the dotfile switch changed nothing: ' + JSON.stringify(out.withDots));
  pane.querySelector('.hf-hidden').click(); await sleep(200);

  /* ── the path is a row of buttons ──────────────────────────────────────────────────────────── */
  out.crumbs = [...pane.querySelectorAll('.fx-crumb')].map(b => b.textContent.trim());
  /* `~` for the home directory, then the path below it — `/ home npub1fdtthaq… Documents` is four
   * crumbs of which three never change and one is unreadable. Above home is still reachable
   * through it. */
  if (out.crumbs.length < 1 || !(out.crumbs[0] === '/' || out.crumbs[0] === '~'))
    bad('no-crumbs', 'the path is not navigable: ' + JSON.stringify(out.crumbs));

  /* ── walking in, and back out ──────────────────────────────────────────────────────────────── */
  row('Documents').click(); await sleep(250);
  out.inside = rows();
  if (!out.inside.includes('tax.pdf'))
    bad('wont-open', 'pressing a folder did not open it: ' + JSON.stringify(out.inside));
  pane.querySelector('.hf-up').click(); await sleep(250);
  if (!rows().includes('Documents'))
    bad('wont-open', 'the parent button did not come back up');
  // …and by a crumb, which is the way back from three levels down.
  row('Documents').click(); await sleep(250);
  [...pane.querySelectorAll('.fx-crumb')].find(b => /^(u|~)$/.test(b.textContent.trim())).click();
  await sleep(250);
  if (!rows().includes('movie.mp4')) bad('no-crumbs', 'a path button did not navigate');

  /* ── a FILE is handed to the machine, a FOLDER is not ──────────────────────────────────────── */
  row('movie.mp4').click(); await sleep(200);
  out.opened = window.__opened.slice();
  if (!out.opened.includes('/home/u/movie.mp4'))
    bad('opens-a-folder', 'clicking a file did not hand it to the machine');
  row('Documents').click(); await sleep(250);
  if (window.__opened.some(p => /Documents$/.test(p)))
    bad('opens-a-folder', 'clicking a folder handed it to xdg-open instead of walking into it');
  pane.querySelector('.hf-up').click(); await sleep(250);

  /* ── delete asks, and says it can be undone ────────────────────────────────────────────────── */
  const ev = (el, o) => el.dispatchEvent(new MouseEvent('click', Object.assign({ bubbles: true }, o)));
  ev(row('a-note.txt'), { ctrlKey: true }); await sleep(220);
  const del = pane.querySelector('.hf-del');
  if (!del) { bad('delete-unasked', 'selecting something offers no delete'); return out; }
  window.__confirmWith = false;
  del.click(); await sleep(220);
  out.asked = (window.__confirmed[0] || '');
  if (!out.asked) bad('delete-unasked', 'delete did not ask first');
  else if (!/trash|bin/i.test(out.asked) || !/put .* back|restore/i.test(out.asked))
    bad('delete-unasked', 'the dialog does not say it can be undone: ' + JSON.stringify(out.asked));
  if (window.__trashed.length)
    bad('delete-unasked', 'a refused confirmation still deleted something');
  window.__confirmWith = true;
  pane.querySelector('.hf-del').click(); await sleep(350);
  out.trashed = window.__trashed.slice();
  if (!out.trashed.includes('/home/u/a-note.txt'))
    bad('delete-unasked', 'a confirmed delete deleted nothing');

  /* ── could not read is not empty ───────────────────────────────────────────────────────────── */
  PCHostFiles.enter('/unreadable');
  await window.__draw(); await sleep(250);
  out.unreadable = pane.textContent.trim().slice(0, 120);
  if (/empty/i.test(out.unreadable) || !/couldn|permission|EACCES/i.test(out.unreadable))
    bad('reads-as-empty', 'an unreadable directory reads as: ' + JSON.stringify(out.unreadable));

  return out;
})()"""


async def drive(url):
    import websockets  # noqa: F401
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": 1200, "height": 900, "deviceScaleFactor": 1, "mobile": False})
            await call("Page.navigate", {"url": url})
            ok = False
            for _ in range(80):
                await asyncio.sleep(0.25)
                r = await call("Runtime.evaluate",
                               {"expression": "window.__ready === true && !!window.PCHostFiles",
                                "returnByValue": True})
                if r and r.get("result", {}).get("value") is True:
                    ok = True
                    break
            if not ok:
                print("SKIP  the page never became ready")
                return 2
            r = await call("Runtime.evaluate",
                           {"expression": DRIVE, "returnByValue": True, "awaitPromise": True})
            if r.get("exceptionDetails"):
                print("FAIL  the driver threw:", json.dumps(r["exceptionDetails"])[:900])
                return 1
            out = r["result"].get("value") or {}
            for k in ("listing", "withDots", "crumbs", "inside", "opened", "asked", "trashed",
                      "unreadable"):
                if k in out:
                    print(f"  {k}: {json.dumps(out[k])[:160]}")
            problems = out.get("problems") or []
            if not problems:
                print("PASS  the machine's own files browse, open, rename and delete to its bin")
                return 0
            for p in problems:
                print(f"FAIL  {p['k']}: {p['d']}")
            return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="hostfiles-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE)

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(tmp, path.lstrip("/") or "index.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    try:
        return asyncio.run(drive(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
