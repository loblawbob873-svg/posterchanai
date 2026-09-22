#!/usr/bin/env python3
"""YOUR OWN FILES MUST BE REACHABLE FROM EVERY SURFACE THAT OFFERS THEM.

    venv-unified/bin/python scripts/check_your_files_are_reachable.py
    venv-unified/bin/python scripts/check_your_files_are_reachable.py --simulate <bug>   # prove it fails

WHY THIS EXISTS. Three bugs shipped in one week and they are the SAME BUG, three times:

  1. THE ATTACH PICKER SHOWED AN EMPTY GRID FOR ALMOST EVERY FOLDER. The Blossom listing is bounded
     to the newest 2000 of 37,483 blobs (deliberately — the full listing is 9.7 MB and made the
     sheet a permanent spinner), and the grid then filtered THAT WINDOW by folder. Measured on a
     real account: Memes 79 files -> 0 shown, Anime 75 -> 0, Blacks 60 -> 0, Jews 60 -> 0,
     Notes 1146 -> 0, Music 2444 -> 0.
  2. THE MUSIC FOLDER WAS IN NO PICKER IN THE APP. `FilesIdx.isEncFolder()` answers true for the
     literal name 'Music' (correct — tracks are stored encrypted) and every picker built its folder
     bar with `!isEncFolder(f)`, so the one folder a Meme Builder user most wants could never be
     opened.
  3. THE DESKTOP WALLPAPER PICKER SAID "No pictures yet." It read the files index SYNCHRONOUSLY and
     never called `ensure()`, so an index that had not been materialised yet was reported to the
     user as an empty folder — an instruction to go and do the thing they had already done.

AND WHY NOTHING CAUGHT ANY OF THEM. `scripts/check_blossom_picker_mobile.py` drives the real picker
and passed throughout; it reports "runtime/layout: clean", which means no JS error and no horizontal
overflow. It never asserts that a FILE IS VISIBLE. `check_files_explorer.py` is a layout check over
sampled markup. The wallpaper picker had no check at all, and `check_os_desktop.py`'s drive stub is
a plain object with no `ensure` at all, so the one code path that broke was not present in it.

A surface that renders perfectly while showing NONE of the user's data is the failure this check is
about, and there is exactly one rule here:

    AN EMPTY GRID IS A FAILURE WHENEVER THE SEEDED INDEX SAYS THAT FOLDER HAS FILES.

"Nothing in this folder.", "No files in X yet", "No pictures yet." — each of those is a sentence the
app prints about a folder it has been told holds files, and each one is red here. A folder that a
surface excludes ON PURPOSE has to prove it: the exclusion must line up with the shipped
`isEncFolder()` (a caller that did not ask for encrypted files), or with the folder having its own
list screen (Music), and the same folder must be present and NON-EMPTY for the caller that did ask.
Tolerating an empty grid is what let all three of these ship.

HOW IT RUNS. No instance, no network, no keys. The functions are LIFTED OUT OF THE SHIPPED app.js
by name and evaluated (the same trick check_files_explorer.py uses) and os.js is loaded whole, so a
renamed function fails this check rather than quietly making it test a copy. The drive is seeded
deliberately OUTSIDE every fast path:

  Memes 79, Anime 75, Notes 46, Backgrounds 7 — all with OLD timestamps, so they fall outside any
  newest-N listing window;  Recent 120 — new, and exactly fills the window, so it is the control
  that proves the window itself works;  Music 40 and Private 12 — encrypted, the folders a picker
  is most likely to drop on purpose and then drop by accident.

The stub Blossom server answers a listing that ASKED FOR A BOUND (`?limit=N`) with a bounded window
and an unbounded request with everything — which is exactly how the real server behaves against a
drive bigger than the window. A caller that stops bounding its listing therefore passes here; a
caller that bounds it and then reads a folder out of the window does not.

PROVING IT CAN FAIL. `--simulate` puts each of the three bugs back, from this harness, without
touching a shipped file:

  --simulate bounded-folder-rows   the picker reads a folder out of the bounded window   (bug 1)
  --simulate hide-encrypted        every picker drops encrypted folders from its bar     (bug 2)
  --simulate wallpaper-no-ensure   the wallpaper picker reads the index without ensure() (bug 3)

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / no websockets).
"""
import argparse
import asyncio
import functools
import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
SCRIPTS = os.path.join(ROOT, "scripts")

# The runner hands every concurrently-running check its own browser port and profile. Hardcoding
# either is how four checks came to share 9473 and attach to one another's Chrome. The HTTP listener
# binds port 0 and reads the number back, which cannot collide with anything by construction.
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9491)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-files-reachable-check"

# A desktop viewport: the windowed desktop refuses to open below 1024px, and that refusal is correct.
VIEWPORT = (1440, 900)

SIMULATIONS = ("bounded-folder-rows", "hide-encrypted", "wallpaper-no-ensure")


# --------------------------------------------------------------------------------------------
# Lifting the shipped code. Never a copy of it.
# --------------------------------------------------------------------------------------------
def lift(src, name):
    """`function <name>(...)` at module indentation, to the first line that is exactly "  }"."""
    head = re.search(r"\n  function " + re.escape(name) + r"\(", src)
    if not head:
        raise SystemExit("FAIL  could not find %s() in app.js. If it was renamed, rename it here "
                         "too rather than letting this check quietly test nothing." % name)
    line_end = src.index("\n", head.start() + 1)
    first = src[head.start() + 1:line_end]
    if first.rstrip().endswith("}") and first.count("{") == first.count("}"):
        return "\n" + first                      # a one-liner: it closes on its own line
    m = re.search(r"\n  function " + re.escape(name) + r"\(.*?\n  \}", src, re.S)
    if not m:
        raise SystemExit("FAIL  %s() in app.js does not close at module indentation" % name)
    return m.group(0)


def lift_re(src, pattern, what):
    m = re.search(pattern, src, re.S)
    if not m:
        raise SystemExit("FAIL  could not lift %s out of app.js — it was renamed or reshaped, so "
                         "this check would be testing a copy of it instead of the real thing." % what)
    return m.group(0)


# Everything the two app.js surfaces below actually run. Order matters only for the consts.
LIFT_FUNCS = ["_fmtBytes", "_fxView", "_fxSort", "_fxCompare", "_fxBlobKey", "_fxBytes", "_fxWhen",
              "_fxType", "_fxFileGlyph", "_fxIcon", "_fxBlobName", "_fxColsHTML", "_fxDetailsRow", "_fxBindCols",
              "_fxFolderCounts", "_fxSideHTML", "_renderFilesGrid", "musicEntries"]
LIFT_CONSTS = ["_FX_COLS", "_FX_KINDS", "_FILES_PAGE"]


def lifted_app_js(sim):
    # app.js plus the modules split out of it: the Explorer (_renderFilesGrid, _fxSideHTML, …) lives
    # in files.js now, where app.js's live bindings are read through a state object (`_S.x`).
    sys.path.insert(0, ROOT)
    from tests.client_source import client_source, state_shims
    src = client_source()

    picker = lift_re(src, r"\n  function blossomPicker\(.*?\n\n  // ---------- Pics:",
                     "blossomPicker()")
    # `isEncFolder` is lifted as a METHOD and dropped straight into the stub index, because the whole
    # point of bug 2 is that this function is RIGHT (Music is encrypted) and its callers were wrong.
    # Restating it here would let a "fix" that makes it lie sail through.
    is_enc = lift_re(src, r"\n    isEncFolder\(name\)\{.*?\n    addFolder\(", "FilesIdx.isEncFolder()")
    is_enc = is_enc[:is_enc.rindex("\n    addFolder(")]
    fx_match = lift_re(src, r"\n  const _fxMatch = \(nm\).*?\n    return [^\n]*\};\n", "_fxMatch")

    body = ["\n".join(lift_re(src, r"\n  const " + re.escape(c) + r" = .*?;\n", c)
                      for c in LIFT_CONSTS),
            fx_match,
            "\n".join(lift(src, n) for n in LIFT_FUNCS),
            picker]

    if sim == "bounded-folder-rows":
        # THE BUG, PUT BACK: a folder is read out of the bounded listing window instead of out of
        # the index that actually knows what is in it. This is the code that shipped.
        anchor = "      const _folderRows = f => {"
        if anchor not in picker:
            raise SystemExit("FAIL  could not find _folderRows in blossomPicker to simulate bug 1")
        body[-1] = picker.replace(
            anchor,
            "      const _folderRows = f => f ? list.filter(b => (FilesIdx.folderOf(b.sha256)||'') === f) : list;\n"
            "      const _folderRowsFixed = f => {", 1)
    if sim == "hide-encrypted":
        # THE BUG, PUT BACK: the folder bar drops every encrypted folder, whatever the caller asked
        # for — so Music was in no picker in the app.
        anchor = "FilesIdx.folders().filter(f=>opts.allowEncrypted||!FilesIdx.isEncFolder(f))"
        if anchor not in body[-1]:
            raise SystemExit("FAIL  could not find the picker's folder bar to simulate bug 2")
        body[-1] = body[-1].replace(anchor, "FilesIdx.folders().filter(f=>!FilesIdx.isEncFolder(f))", 1)

    # The live-state object the lifted module functions read, over this page's own stubs.
    body.append(state_shims("\n".join(body)))
    return "\n".join(body).replace("</script", "<\\/script"), is_enc


# --------------------------------------------------------------------------------------------
# Page 1 — the attach picker and the Files view, against one seeded drive.
# --------------------------------------------------------------------------------------------
APP_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css"></head><body>
<div id="modal-root"></div>
<div class="fx-explorer"><div class="fx-main"><div class="files-grid" id="bl-grid"></div></div></div>
<script src="/static/js/client/sprite.js"></script>
<script>
window.__errors = [];
addEventListener('error', e => window.__errors.push(String((e && e.message) || e)));
addEventListener('unhandledrejection', e => window.__errors.push('unhandled: ' +
  String((e && e.reason && e.reason.message) || (e && e.reason) || '')));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const $  = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const enc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ---------------- THE SEEDED DRIVE ----------------------------------------------------------
 * Every folder here is deliberately OUTSIDE the fast path except `Recent`, which is the control:
 * it is exactly the newest-N window, so if `Recent` is full and everything else is empty, what is
 * being measured is the window and not the drive. */
const WINDOW = 120;                 // what a bounded listing returns — the drive is bigger than it
const OLD = 1500000000, NEW = 1780000000;
const PLAN = [['Memes', 79], ['Anime', 75], ['Notes', 46], ['Backgrounds', 6],
              ['Music', 40], ['Private', 12], ['Recent', WINDOW]];
const SEED = { folders: ['Memes','Anime','Notes','Backgrounds','Music','Private','Recent'],
               encFolders: ['Private'], files: {} };
let _n = 0;
const shaOf = () => (++_n).toString(16).padStart(8, '0').repeat(8);
for (const [folder, howMany] of PLAN) {
  const isEnc = (folder === 'Music' || folder === 'Private');
  const ext = folder === 'Backgrounds' ? '.jpg' : (folder === 'Music' ? '.mp3' : '.png');
  const mime = folder === 'Backgrounds' ? 'image/jpeg'
             : (folder === 'Music' ? 'audio/mpeg' : 'image/png');
  for (let i = 0; i < howMany; i++) {
    const m = { name: folder + '-' + i + ext, folder, mime, size: 1000 + i,
                ts: (folder === 'Recent' ? NEW : OLD) + i };
    if (isEnc) m.enc = true;
    SEED.files[shaOf()] = m;
  }
}
// One NON-image in Backgrounds, so "the wallpaper picker shows every picture" cannot be satisfied by
// a picker that simply shows the whole folder.
SEED.files[shaOf()] = { name: 'notes.txt', folder: 'Backgrounds', mime: 'text/plain',
                        size: 40, ts: OLD };
window.__seedCounts = (() => { const c = {}; for (const s in SEED.files)
  c[SEED.files[s].folder] = (c[SEED.files[s].folder] || 0) + 1; return c; })();
window.__seedBackgroundPics = Object.keys(SEED.files)
  .filter(s => SEED.files[s].folder === 'Backgrounds' && /^image\//.test(SEED.files[s].mime))
  .map(s => SEED.files[s].name).sort();

// The server's listing: newest first, as Blossom serves it.
const ALLROWS = Object.keys(SEED.files).map(sha => ({
  sha256: sha, url: 'https://media.example/' + sha, size: SEED.files[sha].size,
  type: SEED.files[sha].enc ? 'application/octet-stream' : SEED.files[sha].mime,
  uploaded: SEED.files[sha].ts })).sort((a, b) => b.uploaded - a.uploaded);

/* A BOUNDED REQUEST GETS A BOUNDED ANSWER, an unbounded one gets the drive. That asymmetry is the
 * whole reproduction: the picker asks for `?limit=2000` against 37,483 blobs, the Files view asks
 * for everything. A caller that stops bounding its listing passes here — this is not a rule against
 * the bound, it is a rule against reading a folder out of one. */
window.fetch = async (url) => {
  const m = /[?&]limit=(\d+)/.exec(String(url));
  const rows = m ? ALLROWS.slice(0, Math.min(+m[1], WINDOW)) : ALLROWS;
  return { ok: true, status: 200, json: async () => rows };
};

/* ---------------- THE DRIVE INDEX ------------------------------------------------------------
 * LAZY on purpose. The real index is an encrypted document that has to be fetched and decrypted, so
 * reading it before anything asked for it yields nothing — which is bug 3, and it is invisible to
 * any stub that is simply a plain object holding the files. */
window.__idxLoads = 0;
const FilesIdx = {
  _rev: 0, _loaded: false, data: SEED,
  _lastIndexSha: 'f'.repeat(64), _indexShas: new Set(),
  _empty: { folders: [], encFolders: [], files: {} },
  _norm(){ return this._loaded ? this.data : this._empty; },
  _load(){ if(!this._loaded){ this._loaded = true; this._rev++; window.__idxLoads++; } },
  loadLocal(){ this._load(); },                       // what the Files view calls
  async ensure(){ await sleep(40); this._load(); return true; },
  folders(){ return this._norm().folders; },
LIFTED_IS_ENC_FOLDER
  meta(sha){ return this._norm().files[sha] || null; },
  folderOf(sha){ const m = this._norm().files[sha]; return (m && m.folder) || ''; },
  _key(){ return 'stub'; },
};
window.FilesIdx = FilesIdx;

/* ---------------- the rest of app.js, as these functions reach it --------------------------- */
const ME = { pubkey: 'ab'.repeat(32) };
const _MIME_EXT = { 'image/jpeg':'jpg', 'image/png':'png', 'audio/mpeg':'mp3', 'text/plain':'txt' };
const mediaServer = () => 'https://media.example';
const toast = m => (window.__toasts = window.__toasts || []).push(String(m));
const mimeForName = n => /\.png$/.test(n) ? 'image/png' : /\.jpe?g$/.test(n) ? 'image/jpeg'
                       : /\.mp3$/.test(n) ? 'audio/mpeg' : /\.txt$/.test(n) ? 'text/plain' : '';
const extOfBlob = (b, m) => String((m && m.name) || b.name || '').split('.').pop() || '';
const downloadName = (b, nm, ext) => nm || (b.sha256 || '').slice(0, 8);
const fileLabel = n => String(n || '');
const blobThumb = () => '<span class="file-icon">F</span>';   // never a real <img>: 378 of them is a
                                                              // network test, not a content one
const _trapFocus = () => {};
const _popKeys = () => {};
const _bindThumbFallback = () => {};
const _fxBindChipDrop = () => {};
const _filesSelBar = () => {};
const _selEl = () => {};
const _handlersFor = () => [];
const _previewable = () => false;
const _openFileName = d => d.name || '';
const openPreviewFile = () => {};
const trackUrl = async () => '';
const _openWithSheet = () => {};
const saveEncrypted = () => {};
const delBlob = () => {};
const copyUrl = () => {};
const downloadBlobFile = () => {};
const _moveMenu = () => {};
const renameBlob = () => {};
const _fxSyncedHTML = () => '';
const _fxHostHTML = () => '';
const _standalone = () => false;
const IS_ADMIN = false;
const _vodNameMap = {};
const MusicOffline = { _have: null, have: async () => new Set() };
let _filesGridList = null, _filesFolder = '', _filesQ = '', _filesTab = 'public';
let _filesShown = 60, _filesShownFolder = null, _filesDeleted = new Set(), _filesSel = new Set();
let _fxCountsRev = -1, _fxCountsCache = {}, _blobHave = null;
let _hostOn = false, _syncRoot = '', _fxMobileSource = 'blossom', _fxBlossomOpen = true;
const _filesSelClear = () => _filesSel.clear();
window.ClientSettings = { _v: {}, get(k, d){ return k in this._v ? this._v[k] : d; },
                          set(k, v){ this._v[k] = v; } };

/*__LIFTED__*/

/* ---------------- SURFACE 1: the Blossom attach picker -------------------------------------- */
window.__picker = async (allowEncrypted) => {
  $('#modal-root').innerHTML = '';
  document.body.classList.remove('modal-open');
  const opts = { title: 'Attach Files' };
  if (allowEncrypted) opts.allowEncrypted = true;
  blossomPicker({ value: '', dispatchEvent(){} }, () => {}, opts);
  for (let i = 0; i < 100 && !$('#bp-folders .folder-chip'); i++) await sleep(50);
  const chips = $$('#bp-folders .folder-chip');
  if (!chips.length) return { ok: false, why: 'the picker never drew a folder bar' };
  const out = { ok: true, listed: chips.map(c => c.dataset.folder).filter(Boolean),
                shown: {}, empty: {}, all: 0 };
  out.all = $$('#bp-grid .bp-pick-card').length;
  for (const c of chips) {
    const f = c.dataset.folder;
    if (!f) continue;
    c.click();
    await sleep(40);
    out.shown[f] = $$('#bp-grid .bp-pick-card').length;
    const e = $('#bp-grid .empty');
    out.empty[f] = e ? e.textContent.replace(/\s+/g, ' ').trim() : '';
  }
  return out;
};

/* ---------------- SURFACE 2: the Files view ------------------------------------------------- */
window.__filesView = async () => {
  FilesIdx.loadLocal();                       // exactly what renderPublicFiles() does on entry
  const side = document.createElement('div');
  side.innerHTML = _fxSideHTML();
  const out = { ok: true,
                listed: $$('.folder-chip[data-folder]', side).map(c => c.dataset.folder).filter(Boolean),
                counts: _fxFolderCounts(), shown: {}, total: {}, empty: {} };
  const grid = $('#bl-grid');
  for (const f of SEED.folders) {
    _filesFolder = f; _filesShownFolder = null; _filesShown = _FILES_PAGE;
    grid.className = 'files-grid'; grid.innerHTML = '';
    _renderFilesGrid(grid, ALLROWS);
    out.shown[f] = $$('.file-card', grid).length;
    const pc = $('.files-page-count', grid);
    const m = pc && /of (\d+)/.exec(pc.textContent);
    out.total[f] = m ? +m[1] : 0;
    const e = $('.empty', grid);
    out.empty[f] = e ? e.textContent.replace(/\s+/g, ' ').trim() : '';
  }
  // Music has its own list screen — `_renderFilesGrid` drops music ciphertext on purpose. That is a
  // deliberate exclusion only if the screen it is excluded IN FAVOUR OF actually has the tracks.
  const tracks = musicEntries(ALLROWS);
  out.music = { entries: tracks.length, playable: tracks.filter(t => !t.missing).length };
  out.encrypted = {};
  for (const f of SEED.folders) out.encrypted[f] = !!FilesIdx.isEncFolder(f);
  out.errors = window.__errors.slice();
  return out;
};
window.__ready = true;
</script></body></html>"""


# --------------------------------------------------------------------------------------------
# Page 2 — the desktop wallpaper picker, driven through the real os.js.
# --------------------------------------------------------------------------------------------
# The desktop needs a whole stubbed shell (a sidebar to read the app list from, a relay, a signer).
# check_os_desktop.py already has exactly that, correct and maintained, so it is REUSED rather than
# copied — with one substitution: its drive stub is a plain object with no `ensure()` at all, and a
# picker that never calls `ensure()` cannot be caught by a stub that has none.
DESK_IDX_ANCHOR = "window.__PC.filesIdx = () =>"

DESK_IDX = r"""
/* THE DRIVE INDEX, AS IT REALLY BEHAVES: nothing is readable until somebody asks for it. The real
 * index is an encrypted document that is fetched and decrypted on demand, and reading it before
 * that has happened returns nothing — which is not "your Backgrounds folder is empty". */
window.__bgFiles = {};
window.__idxLoaded = false;
window.__idxEnsures = 0;
__SEED_BACKGROUNDS__
window.__PC.filesIdx = () => ({
  _norm: () => (window.__idxLoaded
                  ? { folders: ['Backgrounds', 'Music'], encFolders: [], files: window.__bgFiles }
                  : { folders: [], encFolders: [], files: {} }),
  ensure: async () => { window.__idxEnsures++;
                        await new Promise(r => setTimeout(r, 120));
                        window.__idxLoaded = true; return true; },
});
window.__wallpaper = async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  PCOS.enter();
  await sleep(300);
  const desk = document.querySelector('#os-desk');
  if (!desk) return { ok: false, why: 'the desktop never came up' };
  const r = desk.getBoundingClientRect();
  desk.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true,
                                                     clientX: r.left + 40, clientY: r.bottom - 60 }));
  await sleep(150);
  const row = [...document.querySelectorAll('.os-ctx-b')].find(b => /background/i.test(b.textContent));
  if (!row) return { ok: false, why: 'the desktop menu offers no background row' };
  row.click();
  // The panel says "Reading your drive…" first and then answers. Wait for the ANSWER: a check that
  // reads the placeholder would report whatever the first frame happened to say.
  for (let i = 0; i < 80; i++) {
    await sleep(100);
    const p = document.querySelector('.os-bgpick');
    if (p && !/Reading your drive/.test(p.textContent)) break;
  }
  const pick = document.querySelector('.os-bgpick');
  if (!pick) return { ok: false, why: 'the wallpaper picker never opened' };
  return { ok: true, ensures: window.__idxEnsures,
           tiles: [...pick.querySelectorAll('.os-bg-item')]
                    .map(t => t.getAttribute('title') || 'Default'),
           text: pick.textContent.replace(/\s+/g, ' ').trim().slice(0, 240) };
};
"""


def desktop_page(seed_names, sim):
    sys.path.insert(0, SCRIPTS)
    try:
        import check_os_desktop
    except Exception as exc:                                  # pragma: no cover - environment
        raise SystemExit("SKIP  could not reuse check_os_desktop.py's desktop harness (%s)" % exc)
    page = check_os_desktop.PAGE
    line = next((ln for ln in page.splitlines() if ln.startswith(DESK_IDX_ANCHOR)), None)
    if line is None:
        raise SystemExit("FAIL  check_os_desktop.py no longer stubs the drive as %r, so this check "
                         "cannot replace it with a lazy one." % DESK_IDX_ANCHOR)
    seed = []
    for i, name in enumerate(seed_names):
        mime = "text/plain" if name.endswith(".txt") else "image/jpeg"
        seed.append("window.__bgFiles[%s] = { folder:'Backgrounds', name:%s, mime:%s };"
                    % (json.dumps(chr(97 + i % 26) * 64), json.dumps(name), json.dumps(mime)))
    seed.append("window.__bgFiles[%s] = { folder:'Music', name:'a song', mime:'audio/mpeg' };"
                % json.dumps("z" * 64))
    return page.replace(line, DESK_IDX.replace("__SEED_BACKGROUNDS__", "\n".join(seed)), 1)


# --------------------------------------------------------------------------------------------
OS_JS = os.path.join(ROOT, "static", "js", "client", "os.js")
# BUG 3, PUT BACK, WITHOUT TOUCHING A SHIPPED FILE: the wallpaper picker reads the drive index
# without ever waiting for it to load. Anchored on the smallest stable thing in that expression —
# the test for whether the index can be asked at all — so it survives the surrounding code being
# rewritten (it was, mid-development of this check) and fails LOUDLY when it does not.
OS_NO_ENSURE_ANCHOR = "idx && idx.ensure"


def patched_os_js():
    src = open(OS_JS, encoding="utf-8").read()
    if OS_NO_ENSURE_ANCHOR not in src:
        raise SystemExit("FAIL  os.js no longer asks the drive index to load as %r, so bug 3 cannot "
                         "be simulated — which means this check can no longer be shown to catch it."
                         % OS_NO_ENSURE_ANCHOR)
    return src.replace(OS_NO_ENSURE_ANCHOR, "false && idx.ensure", 1)


def serve(app_html, desk_html, os_js):
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="text/html; charset=utf-8"):
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(app_html)
            if path == "/desktop":
                return self._send(desk_html)
            if path == "/static/js/client/os.js" and os_js is not None:
                return self._send(os_js, "application/javascript; charset=utf-8")
            self.path = path
            return super().do_GET()

    handler = functools.partial(H, directory=ROOT)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def drive(port, sim):
    import websockets
    # The runner hands every concurrent check its own profile directory. Standalone there is no such
    # promise, so fall back to a private temp dir rather than a shared literal two runs could share.
    td = PROFILE if os.environ.get("PC_CHECK_PROFILE") else tempfile.mkdtemp(prefix="pc-files-reach-")
    shutil.rmtree(td, ignore_errors=True)
    os.makedirs(td, exist_ok=True)
    chrome = next((shutil.which(x) for x in
                   ("google-chrome-stable", "chromium", "google-chrome", "chromium-browser")
                   if shutil.which(x)), None)
    if not chrome:
        print("SKIP  no Chrome on this box")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--remote-debugging-port=%d" % CDP_PORT, "--user-data-dir=%s" % td, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tab = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % CDP_PORT))
                tab = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:
                await asyncio.sleep(0.25)
        if not tab:
            print("SKIP  Chrome never opened a debuggable tab")
            return 2

        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=1 << 25) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        if msg.get("error"):
                            raise RuntimeError("%s: %s" % (method, msg["error"]))
                        return msg.get("result") or {}

            async def js(expr):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if r.get("exceptionDetails"):
                    return {"__throw": str((r["exceptionDetails"].get("exception") or {}).get(
                        "description") or r["exceptionDetails"].get("text"))}
                return (r.get("result") or {}).get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": VIEWPORT[0], "height": VIEWPORT[1],
                        "deviceScaleFactor": 1, "mobile": False})

            problems = []
            notes = []

            # ---- pages 1: the picker and the Files view --------------------------------------
            await call("Page.navigate", {"url": "http://127.0.0.1:%d/" % port})
            for _ in range(80):
                await asyncio.sleep(0.1)
                if await js("window.__ready === true"):
                    break
            else:
                print("SKIP  the app.js harness page never finished loading")
                return 2

            seeded = await js("window.__seedCounts")
            pics = await js("window.__seedBackgroundPics")
            if not isinstance(seeded, dict) or not seeded:
                print("SKIP  the harness seeded no drive")
                return 2

            files = await js("window.__filesView()")
            plain = await js("window.__picker(false)")
            withenc = await js("window.__picker(true)")
            for label, got in (("the Files view", files), ("the attach picker", plain),
                               ("the attach picker (allowEncrypted)", withenc)):
                if not isinstance(got, dict) or got.get("__throw"):
                    print("FAIL  %s threw before it could show anything: %s"
                          % (label, (got or {}).get("__throw", got)))
                    return 1
                if not got.get("ok"):
                    print("FAIL  %s: %s" % (label, (got or {}).get("why", got)))
                    return 1

            enc_of = files.get("encrypted") or {}
            folders = sorted(seeded)

            # --- Surface 1a: the picker, as an ordinary caller opens it ----------------------
            missing_plain = [f for f in folders if not enc_of.get(f) and f not in plain["listed"]]
            if missing_plain:
                problems.append(
                    "the attach picker does not LIST %s — a folder that is on the drive and is not "
                    "encrypted has no reason to be absent from a picker"
                    % ", ".join("%s (%d files)" % (f, seeded[f]) for f in missing_plain))
            wrongly_listed = [f for f in folders if enc_of.get(f) and f in plain["listed"]]
            if wrongly_listed:
                problems.append(
                    "the attach picker lists the encrypted folder(s) %s to a caller that did not "
                    "pass allowEncrypted — it would hand out ciphertext URLs nobody can fetch"
                    % ", ".join(wrongly_listed))
            for f in plain["listed"]:
                if plain["shown"].get(f, 0) < 1:
                    problems.append(
                        "the attach picker offers the folder %s and then shows NOTHING in it — the "
                        "seeded drive holds %d files there and the sheet says %r. That is bug 1: a "
                        "folder read out of the bounded listing window instead of out of the index."
                        % (f, seeded.get(f, 0), plain["empty"].get(f) or "(an empty grid)"))
                elif plain["shown"][f] != seeded.get(f):
                    problems.append(
                        "the attach picker shows %d of the %d files in %s — a partial folder is the "
                        "same failure as an empty one, measured from a different angle"
                        % (plain["shown"][f], seeded.get(f, 0), f))

            # --- Surface 1b: the picker, opened by a caller that asked for encrypted files ----
            for f in folders:
                if f not in withenc["listed"]:
                    problems.append(
                        "the folder %s (%d files) is absent even from a picker opened with "
                        "allowEncrypted:true — an exclusion that survives the caller ASKING is not "
                        "deliberate, it is bug 2: `isEncFolder('Music')` is true, so every picker "
                        "that filtered its bar with !isEncFolder(f) made that folder unreachable "
                        "from the whole app." % (f, seeded[f]))
                elif withenc["shown"].get(f, 0) < 1:
                    problems.append(
                        "the attach picker (allowEncrypted) lists %s and shows nothing in it — %d "
                        "files are on the drive and the sheet says %r"
                        % (f, seeded[f], withenc["empty"].get(f) or "(an empty grid)"))
                elif withenc["shown"][f] != seeded[f]:
                    problems.append(
                        "the attach picker (allowEncrypted) shows %d of the %d files in %s"
                        % (withenc["shown"][f], seeded[f], f))

            # --- Surface 2: the Files view ----------------------------------------------------
            for f in folders:
                if f not in files["listed"]:
                    problems.append(
                        "the Files view's own sidebar does not list %s (%d files). The Files view "
                        "shows encrypted folders on purpose — it is where they are managed — so "
                        "there is no caller here that could have asked for it to be dropped."
                        % (f, seeded[f]))
                if files["counts"].get(f, 0) != seeded[f]:
                    problems.append(
                        "the Files home tile for %s counts %s files; the drive index holds %d. The "
                        "tile is the first thing anyone reads about a folder and 'empty' on a full "
                        "one is the whole bug." % (f, files["counts"].get(f, 0), seeded[f]))
                if f == "Music":
                    continue          # its own list screen — asserted below
                if files["shown"].get(f, 0) < 1:
                    problems.append(
                        "opening %s in the Files view shows NOTHING — %d files are indexed there "
                        "and the grid says %r"
                        % (f, seeded[f], files["empty"].get(f) or "(an empty grid)"))
                elif files["total"].get(f, 0) != seeded[f]:
                    problems.append(
                        "the Files view says it is showing %d of %s files in %s; the index holds %d"
                        % (files["shown"].get(f, 0), files["total"].get(f), f, seeded[f]))

            # Music is excluded from the generic grid DELIBERATELY — it has its own list. That is
            # only a deliberate exclusion if the list it is excluded in favour of holds the tracks.
            if files["shown"].get("Music", 0) == 0:
                if files["music"]["entries"] != seeded["Music"]:
                    problems.append(
                        "Music is (rightly) kept out of the ordinary file grid because it has its "
                        "own track list — and that list holds %d of the %d tracks on the drive. An "
                        "exclusion whose replacement is empty is not deliberate."
                        % (files["music"]["entries"], seeded["Music"]))
                elif files["music"]["playable"] != seeded["Music"]:
                    problems.append(
                        "the Music list holds all %d tracks but marks %d of them missing from the "
                        "server — which is the bounded-window bug wearing a different hat: the "
                        "listing the library was checked against did not reach far enough back."
                        % (seeded["Music"], seeded["Music"] - files["music"]["playable"]))
                else:
                    notes.append("Music: kept out of the ordinary grid on purpose; its own list "
                                 "holds all %d tracks, all playable" % seeded["Music"])

            if files.get("errors"):
                problems.append("the Files view logged %s" % (files["errors"][:3],))

            # ---- page 2: the desktop wallpaper picker ----------------------------------------
            await call("Page.navigate", {"url": "http://127.0.0.1:%d/desktop" % port})
            ready = False
            for _ in range(100):
                await asyncio.sleep(0.15)
                if await js("window.__ready === true && !!window.PCOS"):
                    ready = True
                    break
            if not ready:
                print("SKIP  the desktop harness page never finished loading")
                return 2
            wall = await js("window.__wallpaper()")
            if not isinstance(wall, dict) or wall.get("__throw"):
                problems.append("the desktop wallpaper picker threw: %s"
                                % (wall or {}).get("__throw", wall))
            elif not wall.get("ok"):
                problems.append("the desktop wallpaper picker: %s" % wall.get("why"))
            else:
                shown = [t for t in wall["tiles"] if t != "Default"]
                gone = [p for p in pics if p not in shown]
                if gone:
                    problems.append(
                        "the desktop wallpaper picker does not offer %s — %d pictures are in the "
                        "drive's Backgrounds folder and the panel says %r. That is bug 3 when the "
                        "list is empty (%d ensure() call%s): an index nobody asked to load reported "
                        "to the user as an empty folder."
                        % (", ".join(gone), len(pics), wall["text"][:120],
                           wall["ensures"], "" if wall["ensures"] == 1 else "s"))
                extra = [t for t in shown if t not in pics]
                if extra:
                    problems.append(
                        "the desktop wallpaper picker offers %s, which is not a picture — the "
                        "Backgrounds folder holds non-images too and only the images are wallpapers"
                        % ", ".join(extra))
                if not gone and not extra:
                    notes.append("wallpaper picker: all %d pictures in Backgrounds, and only those"
                                 % len(pics))

            if problems:
                print("FAIL  your files are not reachable from every surface that offers them")
                for p in problems:
                    print("  - %s" % p)
                print("\n  seeded drive: %s"
                      % ", ".join("%s %d%s" % (f, seeded[f], " (encrypted)" if enc_of.get(f) else "")
                                  for f in folders))
                print("  re-run: venv-unified/bin/python scripts/check_your_files_are_reachable.py"
                      + ((" --simulate " + sim) if sim else ""))
                return 1

            print("OK    every seeded folder is listed AND non-empty on every surface that shows "
                  "your files")
            print("      drive: %s"
                  % ", ".join("%s %d%s" % (f, seeded[f], " (encrypted)" if enc_of.get(f) else "")
                              for f in folders))
            print("      attach picker: %d folders, %d files in the bounded 'All' window, every "
                  "folder read from the index" % (len(plain["listed"]), plain["all"]))
            print("      attach picker (allowEncrypted): %d folders, encrypted ones included"
                  % len(withenc["listed"]))
            print("      Files view: %d folders in the sidebar, counts match the index"
                  % len(files["listed"]))
            for note in notes:
                print("      %s" % note)
            return 0
    finally:
        proc.terminate()
        shutil.rmtree(td, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--simulate", choices=SIMULATIONS, default=None,
                    help="put one of the three shipped bugs back, to prove this check catches it")
    args = ap.parse_args()

    # A SKIP IS NOT A PASS, so say which dependency is missing rather than dying on the import.
    # Asked with find_spec: `import websockets` here would be an unused name, and the pre-push
    # pyflakes gate is right to call that out.
    if importlib.util.find_spec("websockets") is None:
        print("SKIP  websockets not installed")
        return 2
    if not os.path.exists(APP):
        print("SKIP  static/js/client/app.js is not in this checkout")
        return 2

    lifted, is_enc = lifted_app_js(args.simulate)
    app_html = (APP_PAGE.replace("LIFTED_IS_ENC_FOLDER", is_enc.strip("\n"))
                        .replace("/*__LIFTED__*/", lifted))
    # The Backgrounds names the wallpaper page seeds have to be the ones page 1 asserts about, so
    # read them off the seed rather than restating them.
    pics = ["Backgrounds-%d.jpg" % i for i in range(6)] + ["notes.txt"]
    desk_html = desktop_page(pics, args.simulate)

    os_js = patched_os_js() if args.simulate == "wallpaper-no-ensure" else None
    srv, port = serve(app_html, desk_html, os_js)
    if args.simulate:
        print("SIMULATING the shipped bug %r — this run is EXPECTED to fail.\n" % args.simulate)
    try:
        return asyncio.run(drive(port, args.simulate))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
