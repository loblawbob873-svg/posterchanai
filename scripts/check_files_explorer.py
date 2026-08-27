#!/usr/bin/env python3
"""Layout check for Files → Blossom, the Explorer views, at phone AND desktop widths.

Run BEFORE deploying a Files change:

    venv-unified/bin/python scripts/check_files_explorer.py

check_client_mobile.py runs as a GUEST against the live instance and never opens Files at all, so a
details view whose headings sit over the wrong columns would ship having "passed the mobile check".

THE MARKUP IS NOT WRITTEN HERE. `_fxDetailsRow`, `_fxColsHTML`, `_fxBarHTML` and the sidebar are
lifted out of the shipped app.js by name and evaluated — the same trick tests/client/
test_two_device_sync.py uses for sync.js's pairKey. That matters more than it sounds: this repo has
twice shipped a static repro that passed against CSS the real screen was broken under (the meme
preview, the DM video bubble), because the repro's markup and the app's had drifted. If a function
below is renamed the check FAILS rather than quietly testing a copy.

Assertions, each a way this specific screen breaks:

  horizontal-overflow   the explorer pushes the page sideways. A two-pane layout at 390px is the
                        default failure of every file manager.
  headings-misaligned   a column heading not over its own column. The header and the rows are
                        separate grid containers sharing one template, so a row with one more (or
                        one fewer) cell than the header shifts every heading by a column and the
                        sizes appear under "Type". Checked per column, in pixels.
  sidebar-not-a-column  the folder list is not stacked on desktop — i.e. a selector that matches
                        nothing. Shipped exactly once already: the CSS said `.fx-side
                        .files-folders` and the markup emits `.folder-bar`, so the sidebar rules
                        applied to nothing and the mobile strip never scrolled.
  strip-wraps           on a phone the folder strip must SCROLL sideways, not wrap to three lines —
                        wrapping is the thing the Explorer layout was built to stop.
  text-clipped          a filename, crumb or heading cut off by its own box (scrollWidth beats
                        clientWidth).
  tiny-tap-target       a row, sort heading, crumb or view button under 32px on a phone.
  sort-arrow-missing    the sorted column shows no direction. A sort you cannot see is one you
                        cannot undo.
  columns-hidden-late   Type/Modified still rendered at 390px, where the name has ~120px left.
  row-actions-clipped   the per-row buttons pushed outside the row, i.e. unreachable.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / websockets).
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import base64
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.environ.get("PC_INSTALLED_APP_JS") or os.path.join(
    ROOT, "static", "js", "client", "app.js")
# 390/360: phones. 900: the narrowest desktop, where a 220px sidebar leaves least for the columns.
# 1280: the ordinary case.
WIDTHS = [(390, 844, True), (360, 780, True), (900, 800, False), (1280, 860, False)]
PORT = int(os.environ.get("PC_CHECK_PORT") or 9489)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-files-explorer-check"

# The functions lifted out of app.js. Each is matched from `function <name>(` to the line that closes
# it at the same indentation — app.js indents module-level functions by two spaces, so the closing
# brace is the first line that is exactly "  }".
LIFT = ["_fxDetailsRow", "_fxColsHTML", "_fxBarHTML", "_fxBytes", "_fxWhen", "_fxType", "_fxFileGlyph", "_fxIcon",
        "_fxView", "_fxSort", "_fxCompare"]
# Data the lifted functions close over. Same rule: taken verbatim, never restated here.
LIFT_CONST = ["_FX_COLS", "_FX_KINDS"]


def lift(src, name):
    # app.js indents module-level functions by two spaces, so a function runs to the first line that
    # is exactly "  }".
    m = re.search(r"\n  function " + re.escape(name) + r"\(.*?\n  \}", src, re.S)
    if not m:
        raise SystemExit(
            "could not find %s() in app.js — if it was renamed, rename it here too rather than "
            "letting this check quietly test nothing." % name)
    return m.group(0)


def lift_const(src, name):
    m = re.search(r"\n  const " + re.escape(name) + r" = .*?;\n", src, re.S)
    if not m:
        raise SystemExit("could not find %s in app.js" % name)
    return m.group(0)


def page():
    src = open(APP).read()
    lifted = ("\n".join(lift_const(src, c) for c in LIFT_CONST)
              + "\n" + "\n".join(lift(src, n) for n in LIFT))
    return PAGE_TMPL.replace("/*__LIFTED__*/", lifted)


PAGE_TMPL = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="feed"></div>
<nav class="mobilenav glass"><button class="nav-item"><b>Home</b></button></nav>
<script src="/static/js/client/sprite.js"></script>
<script>
window.__errors=[];
window.addEventListener('error', e=>window.__errors.push(String((e&&e.message)||e)));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// The drive's search box is part of the lifted toolbar, and its query is module state in app.js.
// Empty here: this harness measures LAYOUT, and every assertion is about an unfiltered drive.
let _filesQ = '';
window.ClientSettings = { _v:{filesView:'details', filesSort:{by:'name', dir:1}},
                          get(k,d){ return this._v[k]===undefined?d:this._v[k]; },
                          set(k,v){ this._v[k]=v; } };
/*__LIFTED__*/

// A realistic drive: long names, short names, one of everything the columns have to render.
const FILES = [
  {name:'Quarterly report and appendices (final, revised).pdf', ext:'pdf', size:2411724, at:1785000000},
  {name:'DSC_0001.jpg', ext:'jpg', size:5242880, at:1786000000},
  {name:'a.txt', ext:'txt', size:12, at:1780000000},
  {name:'Stream 3 Aug 2026 14:31.mp4', ext:'mp4', size:918234112, at:1787000000},
  {name:'passwords.kdbx', ext:'kdbx', size:8192, at:1781000000, enc:true},
];
const ICON = '<svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>';
function drive(){
  return FILES.map((f,i) => _fxDetailsRow({
    sha:String(i).repeat(64).slice(0,64), draggable:true, selected:i===1, enc:!!f.enc,
    box:'<input type="checkbox" class="selbox" title="Select">',
    href:'#', encOpen:!!f.enc, mime:'', icon:_fxIcon(f.ext,''), name:f.name, title:f.name,
    size:_fxBytes(f.size), type:(f.enc?'🔒 ':'')+_fxType(f.ext), when:_fxWhen(f.at),
    acts:'<button class="copy" title="Copy URL">⧉</button>'
       + '<button class="dlbtn">'+ICON+'</button><button class="movebtn">'+ICON+'</button>'
       + '<button class="del">'+ICON+'</button>',
  })).join('');
}
// A synced folder: folder rows, and — since a synced file can be picked ONE AT A TIME like every
// other file manager — the same checkbox the drive has. It genuinely had none while the view was
// select-all-or-none, and a repro that keeps the old shape is how an added column passes a check
// that no longer describes the screen. The action counts here MUST match what
// _renderSyncedRoot emits — two buttons on a folder (rename, delete) and four on a file (download,
// keep a copy, rename, delete). They were one and none while the view was read-only, and a repro
// that keeps the old count is how a widened actions column passes a check it no longer describes.
function synced(){
  const EDITS = '<button class="rnsync">'+ICON+'</button><button class="rmsync">'+ICON+'</button>';
  const rows = [
    _fxDetailsRow({dir:true, name:'2026 receipts and invoices', icon:'📁', size:_fxBytes(41231), type:'38 items', when:_fxWhen(1786400000000),
                   box:'<span class="selbox-gap"></span>', acts:EDITS}),
    _fxDetailsRow({name:'notes.md', icon:'📄', size:_fxBytes(1204), type:_fxType('md'), when:_fxWhen(1786400000000),
                   box:'<input type="checkbox" class="selbox syncbox" title="Select">',
                   acts:'<button class="dlsync">'+ICON+'</button><button class="keepsync">'+ICON+'</button>'
                       +'<button class="officesync">'+ICON+'</button><button class="codesync">'+ICON+'</button>'+EDITS}),
  ];
  return rows.join('');
}
const CRUMBS = [{label:'Files', to:'b:'}, {label:'🔄 Documents', to:'s:Documents'},
                {label:'2026 receipts and invoices', to:'s:Documents/2026'}];
const FOLDERS = ['Music','Pictures','Documents','Invoices','Screenshots','Recipes','Old stuff','Work'];
function side(){
  return '<div class="folder-bar">'
    + '<button class="folder-chip active" data-folder="">All</button>'
    + FOLDERS.map(f=>`<button class="folder-chip" data-folder="${f}">📁 ${f}</button>`).join('')
    + '<button class="folder-chip newfolder" id="bl-newfolder">New folder</button></div>'
    + '<div class="fx-sec"><b>Synced folders</b>'
    + '<button class="folder-chip syncroot" data-synckey="Documents">🔄 Documents<span class="fx-n">412</span></button>'
    + '<button class="folder-chip syncroot active" data-synckey="Pictures">🔄 Pictures<span class="fx-n">8213</span></button></div>';
}
function paint(which){
  if(which === 'home'){
    /* The landing view: folder TILES, no columns. Sampled rather than rendered from the real
     * function because the risk here is entirely CSS — a tile grid that overflows a phone, or a long
     * folder name that stretches its track and misaligns the row. Both names below are deliberately
     * awkward for that reason.
     *
     * IT GOES INSIDE #bl-grid, and that is not a detail. _renderDriveHome writes the home INTO the
     * existing `.files-grid` element, which is itself a grid of 150px file-card tracks — so the home
     * is a grid ITEM and lays out in ONE track. This harness used to drop .fx-home straight into
     * .fx-main, where it had the whole pane and looked perfect, while the real screen stacked every
     * folder into a single 150px column that ran off the bottom of the page. A sampled DOM is only
     * worth anything if it is nested the way the real one is. */
    const tile = (ic, name, sub) =>
      '<button class="fx-home-tile"><span class="fx-home-ic">' + ic + '</span>'
      + '<span class="fx-home-name">' + name + '</span>'
      + '<span class="fx-home-sub muted small">' + sub + '</span></button>';
    document.getElementById('feed').innerHTML =
      '<div class="fx-explorer"><div class="fx-side">' + side() + '</div>'
      + '<div class="fx-main">' + _fxBarHTML(CRUMBS.slice(0,1))
      + '<div class="files-grid" id="bl-grid">'
      + '<div class="fx-home">'
      + tile('📁','Posts','128 files') + tile('🎵','Music','2410 files')
      + tile('🔒','Voices','3 files')
      + tile('📁','Screenshots from a very long trip 2024','1 file')
      + '<div class="fx-home-sec">Synced folders</div>'
      + tile('🔄','Documents','15819 files') + tile('🔄','Pictures','6793 files')
      + '<div class="fx-home-sec">Everything</div>'
      + tile('🗂','All files','browse the whole drive')
      + '</div></div></div></div>';
    return;
  }
  const nosel = which === 'synced';
  /* The synced view now mounts an UPLOADER between the toolbar and the grid — a drop zone with a
     button in it. It is part of what has to fit at 390px, so the repro carries it: a check that
     leaves out a block the screen actually renders is measuring a screen nobody has. */
  const DROP = nosel ? '<div class="drop-zone" id="sf-drop"><div class="dz-inner"><span class="dz-ic">\u2b06</span>'
    + ' Drop files here, or <button class="btn btn-cyan small" id="sf-pick">choose files</button>'
    + '<div class="muted small">\u2192 \ud83d\udd04 Documents / 2026 receipts and invoices \u00b7 added to every device that syncs this folder</div>'
    + '</div><div class="up-queue" id="sf-queue"></div></div>' : '';
  document.getElementById('feed').innerHTML =
    '<div class="fx-explorer"><div class="fx-side">' + side() + '</div>'
    + '<div class="fx-main">' + _fxBarHTML(CRUMBS)
    + DROP
    + '<div class="files-grid details" id="bl-grid">'
    + _fxColsHTML(true) + (nosel ? synced() : drive()) + '</div></div></div>';
}
window.__paint = paint;
paint('drive');
window.__ready = true;
</script>
</body></html>"""

AUDIT = r"""(() => {
  const vw = window.innerWidth;
  const box = el => { const r = el.getBoundingClientRect(); return {x:r.x, y:r.y, w:r.width, h:r.height, right:r.right}; };
  const vis = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
  const out = { vw, overflow: document.documentElement.scrollWidth > vw + 1,
                homeTiles: document.querySelectorAll('.fx-home-tile').length };

  /* How many COLUMNS the folder tiles actually landed in, and how wide the tile grid got.
   * "It rendered tiles" was the only thing asked before, and that stayed true while the home was
   * squeezed into one 150px track of its host grid and ran vertically off the page. Distinct x
   * positions is the measurement that tells those two apart. */
  {
    const tiles = Array.from(document.querySelectorAll('.fx-home-tile')).filter(vis);
    out.homeCols = new Set(tiles.map(t => Math.round(box(t).x))).size;
    const home = document.querySelector('.fx-home');
    const host = document.querySelector('.fx-home') && document.querySelector('.fx-home').parentElement;
    out.homeW = home ? Math.round(box(home).w) : 0;
    out.homeHostW = host ? Math.round(box(host).w) : 0;
    out.homeH = home ? Math.round(box(home).h) : 0;
  }

  // Headings over their own columns. The header and each row are SEPARATE grid containers that share
  // a template, so this compares the real laid-out x of each header cell with the row cell that
  // should sit under it.
  /* EVERY row, not just the first. Each row is its own grid container, so a column sized to its
     content resolves per row: a row with four action buttons, an encrypted row with three and a
     folder row with none each land somewhere different, and checking one of them finds at most one
     of those. That is how this shipped misaligned the first time. */
  const cols = document.querySelector('.fx-cols');
  const rows = Array.from(document.querySelectorAll('.file-card.row'));
  out.hasCols = !!cols; out.hasRow = rows.length > 0;
  out.align = []; out.cellCounts = [];
  if(cols && rows.length){
    const hs = Array.from(cols.children).filter(vis);
    out.headerCells = hs.length;
    for(const row of rows){
      const rs = Array.from(row.children).filter(vis);
      out.cellCounts.push(rs.length);
      const who = ((row.querySelector('.fname')||{}).textContent || '').trim().slice(0, 14);
      for(let i = 0; i < Math.min(hs.length, rs.length); i++){
        // Only cells that CARRY a heading. The first and last are spacers holding the checkbox and
        // the row buttons in their columns — a checkbox centred in its 24px cell and a right-aligned
        // button group are both correct, and comparing them to an empty span measures nothing.
        if(!(hs[i].textContent||'').trim()) continue;
        const dx = Math.round(box(hs[i]).x - box(rs[i]).x);
        if(Math.abs(dx) > 2) out.align.push({ i, who, head:(hs[i].textContent||'').trim().slice(0,10), dx });
      }
    }
    out.rowCells = Math.max.apply(null, out.cellCounts);
  }
  // The sorted column has to SAY which way.
  const on = document.querySelector('.fx-col.on');
  out.sortedCol = on ? (on.textContent||'').trim() : '';
  out.sortArrow = on ? (on.querySelector('.fx-arrow')||{}).textContent || '' : '';

  // The sidebar: a column on desktop, a sideways-scrolling strip on a phone. Two chips sharing a y
  // means a row; two chips sharing an x means a column.
  const chips = Array.from(document.querySelectorAll('.fx-side .folder-bar .folder-chip')).filter(vis);
  out.chips = chips.length;
  if(chips.length > 1){
    const a = box(chips[0]), b = box(chips[1]);
    out.chipsStacked = Math.abs(a.x - b.x) < 2 && b.y > a.y + 1;
    out.chipsInARow  = Math.abs(a.y - b.y) < 2;
  }
  const bar = document.querySelector('.fx-side .folder-bar');
  out.stripScrolls = bar ? bar.scrollWidth > bar.clientWidth + 1 : false;
  // Wrapping shows up as chips on more than two distinct rows in a strip that does NOT scroll.
  out.chipRows = new Set(chips.map(c => Math.round(box(c).y))).size;

  // Which columns are actually drawn.
  out.showsType = vis(document.querySelector('.fx-type'));
  out.showsMod  = vis(document.querySelector('.fx-mod'));

  out.clipped = [];
  for(const el of document.querySelectorAll('.fx-col, .fx-crumb, .fname, .fx-size, .fx-type, .fx-mod, .folder-chip')){
    if(!vis(el)) continue;
    const t = (el.textContent||'').trim();
    /* Text that is DELIBERATELY ellipsised is not clipped text: a filename column and a breadcrumb
       both hold arbitrary names, and shortening them with an ellipsis is what they are for. A label
       cut off with no ellipsis is a layout that ran out of room and did not say so — which is the
       thing worth failing on. So the exemption is the STYLE, not a list of class names. */
    const cs = getComputedStyle(el);
    if(cs.textOverflow === 'ellipsis' && cs.whiteSpace === 'nowrap') continue;
    if(t && el.scrollWidth > el.clientWidth + 2)
      out.clipped.push({ cls:el.className, text:t.slice(0,22), shown:Math.round(el.clientWidth), needs:Math.round(el.scrollWidth) });
  }
  out.small = [];
  for(const el of document.querySelectorAll('.file-card.row, .fx-col, .fx-crumb, .fx-vw, .folder-chip')){
    if(!vis(el)) continue;
    const b = box(el);
    if(b.h < 32) out.small.push({ cls:el.className, h:Math.round(b.h), text:(el.textContent||'').trim().slice(0,18) });
  }
  // Row actions inside their row.
  out.actsOut = [];
  for(const a of document.querySelectorAll('.file-card.row .fc-acts')){
    if(!vis(a)) continue;
    const r = a.closest('.file-card.row');
    if(box(a).right > box(r).right + 1) out.actsOut.push(Math.round(box(a).right - box(r).right));
  }
  // Nothing may reach past the pane it lives in.
  const main = document.querySelector('.fx-main');
  out.rowsOut = main ? Array.from(document.querySelectorAll('.file-card.row'))
    .filter(r => box(r).right > box(main).right + 1).length : 0;
  return out;
})()"""


async def drive_browser(url):
    import websockets
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium") or shutil.which("chromium-browser"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page_tab = None
    try:
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page_tab = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page_tab:
            print("SKIP  could not start Chrome")
            return 2

        problems = []
        async with websockets.connect(page_tab["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                if r.get("exceptionDetails"):
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for w, h, phone in WIDTHS:
                for which in ("home", "drive", "synced"):
                    label = f"{w}px/{which}"
                    await call("Emulation.setDeviceMetricsOverride",
                               {"width": w, "height": h, "deviceScaleFactor": 2 if phone else 1,
                                "mobile": phone})
                    await call("Page.navigate", {"url": url})
                    ok = False
                    for _ in range(60):
                        await asyncio.sleep(0.2)
                        if await js("window.__ready === true"):
                            ok = True
                            break
                    if not ok:
                        errors = await js("window.__errors || []")
                        print(f"SKIP  {label}: the page never rendered ({errors})")
                        return 2
                    await js(f"window.__paint({json.dumps(which)})")
                    await asyncio.sleep(0.15)
                    shots = os.environ.get("PC_CHECK_SHOTS")
                    if shots:
                        os.makedirs(shots, exist_ok=True)
                        shot = await call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
                        with open(os.path.join(shots, f"files-{w}-{which}.png"), "wb") as fh:
                            fh.write(base64.b64decode(shot["data"]))
                    r = await js(AUDIT)
                    if r is None:
                        print(f"SKIP  {label}: page did not evaluate")
                        return 2
                    problems += judge(label, r, phone, which)

        if problems:
            print("\nFiles Explorer — regressions:\n")
            for p in problems:
                print("  " + p)
            return 1
        print("Files Explorer: clean at " + ", ".join(f"{w}px" for w, _, _ in WIDTHS)
              + " · home, drive and synced views")
        return 0
    finally:
        proc.terminate()


def judge(label, r, phone, which):
    bad = []
    if r["overflow"]:
        bad.append(f"[horizontal-overflow] {label}: the page scrolls sideways")
    if which == "home":
        # No columns here by design — the drive's landing view is folder tiles. What can go wrong is
        # the grid overflowing (checked above), a name stretching its track, or nothing rendering.
        if not r.get("homeTiles"):
            bad.append(f"[home-empty] {label}: the drive home drew no folder tiles at all")
        # The home is written into #bl-grid, a grid whose tracks are 150px file cards. If it is
        # laid out as one ITEM of that grid it gets a single track, falls to one column, and every
        # folder stacks — which is what shipped, and what "it drew tiles" could not see. It has to
        # take the host's full width.
        elif r.get("homeHostW") and r["homeW"] < r["homeHostW"] * 0.8:
            bad.append(f"[home-squeezed] {label}: the tile grid is {r['homeW']}px inside a "
                       f"{r['homeHostW']}px host — it is sitting in one track of #bl-grid, not spanning it")
        # A phone is one column by design; a desktop pane fits several, and one there means the
        # same squeeze by another name.
        elif not phone and r.get("homeCols", 0) < 2:
            bad.append(f"[home-onecolumn] {label}: {r['homeTiles']} folder tiles in "
                       f"{r.get('homeCols')} column ({r.get('homeH')}px tall) — the grid is not laying out")
        for c in r["clipped"]:
            bad.append(f"[text-clipped] {label}: “{c['text']}” needs {c['needs']}px, has {c['shown']} ({c['cls']})")
        return bad
    if not r["hasCols"] or not r["hasRow"]:
        bad.append(f"[headings-misaligned] {label}: no column header or no rows rendered at all")
    else:
        for count in set(r.get("cellCounts") or []):
            if count != r.get("headerCells"):
                bad.append(f"[headings-misaligned] {label}: the header has {r['headerCells']} cells and a "
                           f"row has {count} — every heading is over the wrong column")
        seen = set()
        for a in r["align"]:
            key = (a["head"], a["dx"])
            if key in seen:
                continue          # the same drift on every row is one fault, not five
            seen.add(key)
            bad.append(f"[headings-misaligned] {label}: “{a['head']}” sits {a['dx']}px off its column "
                       f"(row “{a['who']}”)")
    if not r["sortArrow"].strip():
        bad.append(f"[sort-arrow-missing] {label}: the sorted column “{r['sortedCol']}” shows no direction")
    if r["chips"] > 1:
        if phone:
            if not r.get("chipsInARow"):
                bad.append(f"[strip-wraps] {label}: the folder strip is not a single row on a phone")
            elif r["chipRows"] > 1 and not r["stripScrolls"]:
                bad.append(f"[strip-wraps] {label}: the folder strip wraps to {r['chipRows']} lines "
                           "instead of scrolling sideways")
        elif not r.get("chipsStacked"):
            bad.append(f"[sidebar-not-a-column] {label}: the folder list is not stacked — a sidebar "
                       "selector that matches nothing looks exactly like this")
    if phone and (r["showsType"] or r["showsMod"]):
        shown = " and ".join(x for x, on in (("Type", r["showsType"]), ("Modified", r["showsMod"])) if on)
        bad.append(f"[columns-hidden-late] {label}: {shown} still drawn at {r['vw']}px, "
                   "leaving the name nothing")
    for c in r["clipped"]:
        bad.append(f"[text-clipped] {label}: “{c['text']}” needs {c['needs']}px, has {c['shown']} ({c['cls']})")
    if phone:
        for s in r["small"]:
            bad.append(f"[tiny-tap-target] {label}: {s['h']}px — “{s['text']}” ({s['cls']})")
    for over in r["actsOut"]:
        bad.append(f"[row-actions-clipped] {label}: the row buttons sit {over}px past the row")
    if r["rowsOut"]:
        bad.append(f"[horizontal-overflow] {label}: {r['rowsOut']} row(s) reach past the file pane")
    return bad


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="fxcheck-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(page())

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
    url = f"http://127.0.0.1:{srv.server_port}/index.html"
    try:
        return asyncio.run(drive_browser(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
