#!/usr/bin/env python3
"""Find, replace and search-in-files in POSTERCHAN CODE, in a real browser.

    venv-unified/bin/python scripts/check_code_find_replace.py

Drives the real code.js against a stubbed `window.__PC` and a stubbed /api/code/* (no server, no
login), like check_code_editor.py, and measures what only a browser can answer:

  find-bar            Ctrl+F opens the bar prefilled from the selection, the count is right, every
                      match is drawn in the highlight layer with exactly one "current" match, and a
                      match far down the file is scrolled INTO VIEW (the textarea and the layers
                      scroll together).
  next-prev-wrap      Enter / Shift+Enter walk the matches and WRAP at both ends, and the textarea's
                      selection is the match the counter names.
  toggles             Match case, Whole word and Regex change the count; an invalid regex says so.
  replace             Replace changes the current match and advances; Replace all changes every
                      match, reports the count, marks the tab dirty — and ONE Ctrl+Z (the browser's
                      own undo, execCommand('undo')) restores the file exactly. A regex replace
                      expands $1.
  esc-focus           Esc closes the bar and gives focus back to the FILE with the match selected.
  search-in-files     Ctrl+Shift+H searches the workspace (walking into a sub-folder, skipping a
                      binary), and clicking a hit in ANOTHER file opens it with the match selected.
  phone-width         At 360px the bar fits the screen, its input is usable, nothing scrolls sideways.
  theme               In a light theme the counter is readable against the bar.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / websockets).
"""
import asyncio
import re as _re
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9491)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-code-find-check"

# 12 x "needle" (3 capitalised) spread down a file long enough to scroll; the last is on line ~90.
LINES = ["import os", "needle_1 = 1", "# Needle in a comment", "def f(needle_2):", "    return needle_2"]
LINES += ["    x = %d  # filler line long enough to matter" % i for i in range(80)]
LINES += ["NEEDLE_3 = 'needle haystack needles'", "needle_4 = needle_5", "print(Needle)", ""]
MAIN = "\n".join(LINES)
FILES = {
    "main.py": MAIN,
    "other.py": "a = 1\nb = 2\n",
    "sub/deep.py": "x = 1\n\n# the deepword lives here\ny = deepword + 1\n",
}

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div class="app" style="display:flex;flex-direction:column;height:100dvh">
  <div id="feed" class="feed"></div>
</div>
<div id="modal-root"></div><div id="toast-root"></div>
<script>
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.__view = 'code';
window.__FILES = __FILES__;
window.__fetched = [];
const ent = (name, dir, size) => ({name, dir, size: size||0, mtime: 1, lang: ''});
window.fetch = async (url, opts) => {
  const u = String(url); const j = d => ({ ok:true, status:200, json: async()=>d });
  const q = decodeURIComponent((u.split('path=')[1]||'').split('&')[0]);
  if(u.startsWith('/api/code/config')) return j({ root:'/srv/workspace', engines:{}, maxBytes:2097152 });
  if(u.startsWith('/api/code/tree')){
    if(q === 'sub') return j({ path:'sub', truncated:false, entries:[ent('deep.py', false, 60)] });
    return j({ path:'', truncated:false, entries:[ent('sub', true), ent('main.py', false, 3000),
      ent('other.py', false, 12), ent('logo.png', false, 900), ent('huge.log', false, 5*1024*1024)] });
  }
  if(u.startsWith('/api/code/file') && (!opts || opts.method !== 'POST')){
    window.__fetched.push(q);
    if(!(q in window.__FILES)) return { ok:false, status:404, json: async()=>({detail:'no such file'}) };
    return j({ path:q, text: window.__FILES[q], lang:'python', size:1, mtime:2 });
  }
  return j({ ok:true });
};
window.__PC = {
  $, $$, enc, toast: () => {}, authFetch: (u,o) => window.fetch(u,o),
  ensureAiSession: async () => ({ can_ai:true, is_admin:true }),
  uiPrompt: async () => '', uiConfirm: async () => true,
  switchView: v => { window.__view = v; },
  get ME(){ return {pubkey:'abcdef012345'}; },
  get VIEW(){ return window.__view; },
};
window.__key = (el, key, o) => { const ev = new KeyboardEvent('keydown', Object.assign({key, bubbles:true, cancelable:true}, o||{}));
  el.dispatchEvent(ev); return ev.defaultPrevented; };
window.__sleep = ms => new Promise(r => setTimeout(r, ms));
window.__type = async (sel, v) => { const el = $(sel); el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); await __sleep(60); };
window.__st = () => { const ta = $('#pcc-ta'), n = $('#pcc-f-n');
  return { count: n ? n.textContent : null, marks: $$('#pcc-fm mark').length, cur: $$('#pcc-fm mark.cur').length,
           sel: ta ? ta.value.slice(ta.selectionStart, ta.selectionEnd) : null,
           selAt: ta ? ta.selectionStart : -1, bar: !!$('#pcc-find') }; };
</script>
<script src="/static/js/client/hostfiles.js"></script>
<script src="/static/js/client/code.js"></script>
<script>
(async function(){
  for(let i=0;i<80 && !window.PCCode;i++) await new Promise(r=>setTimeout(r,50));
  await window.PCCode.render();
  window.__ready = true;
})();
</script>
</body></html>"""

OPEN_MAIN = r"""(async () => {
  const t = document.querySelector('[data-file="main.py"]');
  if(!t) return {error:'no main.py in the tree'};
  t.click();
  for(let i=0;i<60 && !document.querySelector('#pcc-ta');i++) await __sleep(50);
  const ta = document.querySelector('#pcc-ta');
  return { opened: !!ta, original: ta ? ta.value : '' };
})()"""

FIND = r"""(async () => {
  const ta = $('#pcc-ta'), out = {};
  // Select the word "needle" on line 2 and press Ctrl+F: the bar opens prefilled with it.
  ta.focus(); const at = ta.value.indexOf('needle'); ta.setSelectionRange(at, at + 6);
  out.prevented = __key(ta, 'f', {ctrlKey:true});
  await __sleep(80);
  out.open = __st();
  out.prefill = ($('#pcc-f-q')||{}).value;
  out.focusFind = document.activeElement === $('#pcc-f-q');
  // Walk forward all the way round: N presses of Enter wrap back to "1 of N".
  const q = $('#pcc-f-q'), seen = [];
  const n = $$('#pcc-fm mark').length;
  for(let i = 0; i < n; i++){ __key(q, 'Enter'); await __sleep(20); seen.push(__st()); }
  out.walk = seen.map(s => s.count);
  out.walkSel = seen.map(s => s.sel);
  // The last match (line ~89) must be SCROLLED INTO VIEW when it is current.
  __key(q, 'Enter', {shiftKey:true}); await __sleep(40);   // back from 1 → wraps to N
  out.wrapBack = __st().count;
  // Opening the bar repainted the editor: the textarea is a NEW element now.
  const cur = $('#pcc-fm mark.cur'), hl = $('#pcc-hl'), ta2 = $('#pcc-ta');
  out.curVisible = !!cur && cur.offsetTop >= ta2.scrollTop && cur.offsetTop + cur.offsetHeight <= ta2.scrollTop + ta2.clientHeight;
  out.scrolled = ta2.scrollTop;
  out.layersTogether = [$('#pcc-fm').scrollTop, hl.scrollTop, ta2.scrollTop];
  out.curBg = cur ? getComputedStyle(cur).backgroundColor : '';
  out.otherBg = ($$('#pcc-fm mark:not(.cur)')[0] && getComputedStyle($$('#pcc-fm mark:not(.cur)')[0]).backgroundColor) || '';
  // Toggles.
  const tog = async k => { $('[data-fo="' + k + '"]').click(); await __sleep(40); return __st().count; };
  out.caseOn = await tog('cs');
  out.wordOn = await tog('ww');
  out.caseOff = await tog('cs');
  out.wordOff = await tog('ww');
  await __type('#pcc-f-q', 'needle_(');
  out.reOn = await tog('re');
  out.badErr = $('#pcc-f-n').classList.contains('err');
  await __type('#pcc-f-q', 'needle_(\\d)');
  out.reCount = __st().count;
  // Ctrl+F again FROM the find box must not replace the regex with the escaped current match.
  __key($('#pcc-f-q'), 'f', {ctrlKey:true}); await __sleep(40);
  out.reKept = $('#pcc-f-q').value;
  // …nor may F3 IN THE FILE (which selects the match, focus left in the file) then Ctrl+F there.
  const ta3 = $('#pcc-ta'); ta3.focus(); __key(ta3, 'F3'); await __sleep(40);
  __key(ta3, 'f', {ctrlKey:true}); await __sleep(60);
  out.reKeptF3 = $('#pcc-f-q').value;
  out.reOff = await tog('re');
  return out;
})()"""

REPLACE = r"""(async () => {
  const out = {};
  const original = $('#pcc-ta').value;
  __key($('#pcc-f-q'), 'h', {ctrlKey:true}); await __sleep(80);
  // Showing the replace row repaints the editor: the textarea is a NEW element from here on.
  const ta = $('#pcc-ta');
  out.replaceBox = !!$('#pcc-f-r');
  await __type('#pcc-f-q', 'needle');
  const before = __st().count;
  await __type('#pcc-f-r', 'pin');
  // Replace: current match replaced, then the counter advances onto the NEXT one.
  __key($('#pcc-f-r'), 'Enter'); await __sleep(60);
  out.afterOne = __st().count; out.before = before;
  out.oneChanged = (ta.value.match(/pin/g)||[]).length;
  out.stillInReplace = document.activeElement === $('#pcc-f-r');
  // Undo that, then Replace all.
  ta.focus(); document.execCommand('undo'); await __sleep(60);
  out.undoOne = ta.value === original;
  $('#pcc-f-q').focus();
  $('#pcc-f-ra').click(); await __sleep(80);
  out.allText = ta.value;
  out.status = ($('#pcc-status')||{}).textContent || '';
  out.dirty = !!document.querySelector('.pcc-tab.on.dirty');
  out.saveEnabled = !($('#pcc-save')||{}).disabled;
  out.countAfterAll = __st().count;
  // ONE undo restores the whole file.
  ta.focus(); document.execCommand('undo'); await __sleep(80);
  out.undoAll = ta.value === original;
  out.undoCount = __st().count;
  // A regex replace with a capture group.
  $('[data-fo="re"]').click(); await __sleep(30);
  await __type('#pcc-f-q', 'needle_(\\d)');
  await __type('#pcc-f-r', 'pin$1x');
  $('#pcc-f-ra').click(); await __sleep(80);
  out.reText = ta.value;
  ta.focus(); document.execCommand('undo'); await __sleep(60);
  out.undoRe = ta.value === original;
  $('[data-fo="re"]').click(); await __sleep(30);
  return out;
})()"""

ESC = r"""(async () => {
  const ta = $('#pcc-ta');
  await __type('#pcc-f-q', 'NEEDLE_3');
  const want = __st();
  __key($('#pcc-f-q'), 'Escape'); await __sleep(80);
  const t = $('#pcc-ta');
  return { want, bar: !!$('#pcc-find'), focused: document.activeElement === t,
           sel: t.value.slice(t.selectionStart, t.selectionEnd), marks: $$('#pcc-fm mark').length };
})()"""

SEARCH = r"""(async () => {
  const ta = $('#pcc-ta'), out = {};
  ta.focus(); ta.setSelectionRange(0, 0);
  out.prevented = __key(ta, 'H', {ctrlKey:true, shiftKey:true}); await __sleep(80);
  out.panel = !!$('#pcc-sf-q');
  out.focus = document.activeElement === $('#pcc-sf-q');
  await __type('#pcc-sf-q', 'deepword');
  __key($('#pcc-sf-q'), 'Enter');
  for(let i=0;i<80 && !$('[data-sf]');i++) await __sleep(50);
  await __sleep(100);
  out.hits = $$('[data-sf]').map(b => b.textContent);
  out.files = $$('.pcc-sf-file').map(f => f.getAttribute('title'));
  out.note = (document.querySelector('.pcc-sf-note')||{}).textContent || '';
  out.fetched = window.__fetched.slice();
  const hit = $$('[data-sf]')[1] || $$('[data-sf]')[0];
  if(hit) hit.click();
  for(let i=0;i<60;i++){ const t = document.querySelector('.pcc-tab.on .pcc-tabname');
    if(t && t.textContent === 'deep.py') break; await __sleep(50); }
  await __sleep(120);
  const t2 = $('#pcc-ta');
  out.tab = (document.querySelector('.pcc-tab.on .pcc-tabname')||{}).textContent;
  out.sel = t2 ? t2.value.slice(t2.selectionStart, t2.selectionEnd) : '';
  out.line = t2 ? t2.value.slice(0, t2.selectionStart).split('\n').length : 0;
  out.focused = document.activeElement === t2;
  out.stillSearch = !!$('#pcc-sf-q');
  return out;
})()"""

PHONE = r"""(async () => {
  const t = document.querySelector('[data-code-view="explorer"]'); if(t) t.click(); await __sleep(80);
  const f = document.querySelector('[data-file="main.py"]'); if(f) f.click(); await __sleep(150);
  const ta = $('#pcc-ta'); ta.focus();
  __key(ta, 'h', {ctrlKey:true}); await __sleep(100);
  const bar = $('#pcc-find'); if(!bar) return {error:'no bar at phone width'};
  const r = bar.getBoundingClientRect(), q = $('#pcc-f-q').getBoundingClientRect(), rr = $('#pcc-f-r').getBoundingClientRect();
  const btns = $$('#pcc-find button').map(b => b.getBoundingClientRect()).filter(b => b.right > innerWidth + 1 || b.left < -1).length;
  return { left: r.left, right: r.right, vw: innerWidth, qw: q.width, rw: rr.width, off: btns,
           sw: document.documentElement.scrollWidth };
})()"""

THEME = r"""(() => {
  const n = $('#pcc-f-n'), bar = $('#pcc-find');
  if(!n || !bar) return {error:'no bar'};
  return { fg: getComputedStyle(n).color, bg: getComputedStyle(bar).backgroundColor,
           input: getComputedStyle($('#pcc-f-q')).color, ibg: getComputedStyle($('#pcc-f-q')).backgroundColor };
})()"""


def expected_needles(text, case=False, word=False):
    import re
    flags = 0 if case else re.I
    pat = r"(?<!\w)needle(?!\w)" if word else "needle"
    return len(re.findall(pat, text, flags))


async def drive(url):
    import websockets
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
    problems = []

    def bad(kind, msg):
        problems.append((kind, msg))

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

            async def js(expr, awaited=True):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    return {"error": json.dumps(r["exceptionDetails"])[:600]}
                return (r.get("result") or {}).get("value")

            async def viewport(w, h, mobile=False):
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": mobile})

            await call("Page.enable")
            await call("Runtime.enable")
            await viewport(1280, 860)
            await call("Page.navigate", {"url": url})
            for _ in range(120):
                if await js("!!window.__ready", awaited=False):
                    break
                await asyncio.sleep(0.25)
            else:
                print("SKIP  the page never became ready")
                return 2

            got = await js(OPEN_MAIN)
            if not got or got.get("error") or not got.get("opened"):
                print(f"FAIL  could not open a file ({(got or {}).get('error')})")
                return 1
            total = expected_needles(MAIN)

            f = await js(FIND)
            if not f or f.get("error"):
                bad("find-bar", f"the find flow threw: {(f or {}).get('error')}")
            else:
                o = f["open"]
                if not f["prevented"] or not o["bar"]:
                    bad("find-bar", "Ctrl+F did not open the find bar")
                if f["prefill"] != "needle":
                    bad("find-bar", f"the bar was not prefilled from the selection ({f['prefill']!r})")
                if not f["focusFind"]:
                    bad("find-bar", "focus did not move to the find box")
                if o["count"] != f"1 of {total}":
                    bad("find-bar", f"count reads {o['count']!r}, expected '1 of {total}'")
                if o["marks"] != total or o["cur"] != 1:
                    bad("find-bar", f"{o['marks']} matches drawn ({o['cur']} current), expected {total} (1)")
                want = [f"{(i + 1) % total + 1} of {total}" for i in range(total)]
                if f["walk"] != want:
                    bad("next-prev-wrap", f"Enter walked {f['walk']} — expected {want}")
                if any(s.lower() != "needle" for s in f["walkSel"]):
                    bad("next-prev-wrap", f"the file's selection is not the match: {f['walkSel']}")
                if f["wrapBack"] != f"{total} of {total}":
                    bad("next-prev-wrap", f"Shift+Enter from the first did not wrap to the last ({f['wrapBack']!r})")
                if not f["curVisible"] or f["scrolled"] <= 0:
                    bad("find-bar", f"the last match was not scrolled into view (scrollTop {f['scrolled']})")
                if len(set(f["layersTogether"])) != 1:
                    bad("find-bar", f"the highlight layers do not scroll with the file {f['layersTogether']}")
                if not f["curBg"] or f["curBg"] in ("rgba(0, 0, 0, 0)", f["otherBg"]):
                    bad("find-bar", "the current match is not drawn differently from the others")
                case_n = expected_needles(MAIN, case=True)
                word_case = len(_re.findall(r"(?<!\w)needle(?!\w)", MAIN))
                word_n = expected_needles(MAIN, word=True)
                if not (f["caseOn"] or "").endswith(f"of {case_n}"):
                    bad("toggles", f"Match case gave {f['caseOn']!r}, expected {case_n} matches")
                if not (f["wordOn"] or "").endswith(f"of {word_case}"):
                    bad("toggles", f"Match case + Whole word gave {f['wordOn']!r}, expected {word_case}")
                if not (f["caseOff"] or "").endswith(f"of {word_n}"):
                    bad("toggles", f"Whole word gave {f['caseOff']!r}, expected {word_n}")
                if not (f["wordOff"] or "").endswith(f"of {total}"):
                    bad("toggles", f"toggles off gave {f['wordOff']!r}, expected {total}")
                if "invalid" not in (f["reOn"] or "").lower() or not f["badErr"]:
                    bad("toggles", f"an invalid regex did not say so ({f['reOn']!r})")
                re_n = len(_re.findall(r"needle_\d", MAIN, _re.I))
                if not (f["reCount"] or "").endswith(f"of {re_n}"):
                    bad("toggles", f"regex needle_(\\d) gave {f['reCount']!r}, expected {re_n} matches")
                if f.get("reKeptF3") != "needle_(\\d)":
                    bad("toggles", f"F3 in the file then Ctrl+F rewrote the regex to {f.get('reKeptF3')!r}")
                if f["reKept"] != "needle_(\\d)":
                    bad("toggles", f"Ctrl+F from the find box rewrote the query to {f['reKept']!r}")

            r = await js(REPLACE)
            if not r or r.get("error"):
                bad("replace", f"the replace flow threw: {(r or {}).get('error')}")
            else:
                if not r["replaceBox"]:
                    bad("replace", "Ctrl+H did not show the replace field")
                if r["oneChanged"] != 1 or not (r["afterOne"] or "").endswith(f"of {total - 1}"):
                    bad("replace", f"Replace changed {r['oneChanged']} match(es), counter {r['afterOne']!r}")
                if not r["stillInReplace"]:
                    bad("replace", "Replace took focus out of the replace field")
                if not r["undoOne"]:
                    bad("replace", "Ctrl+Z did not undo a single Replace")

                expect_all = _re.sub("needle", "pin", MAIN, flags=_re.I)
                if r["allText"] != expect_all:
                    bad("replace", "Replace all did not change exactly the counted matches")
                if f"Replaced {total}" not in r["status"]:
                    bad("replace", f"Replace all did not report its count ({r['status']!r})")
                if not r["dirty"] or not r["saveEnabled"]:
                    bad("replace", "after Replace all the tab is not marked unsaved")
                if r["countAfterAll"] != "No results":
                    bad("replace", f"counter after Replace all reads {r['countAfterAll']!r}")
                if not r["undoAll"]:
                    bad("replace", "ONE Ctrl+Z did not restore the file after Replace all")
                if r["undoCount"] != f"1 of {total}" and not (r["undoCount"] or "").endswith(f"of {total}"):
                    bad("replace", f"the counter did not follow the undo ({r['undoCount']!r})")
                expect_re = _re.sub(r"needle_(\d)", r"pin\1x", MAIN, flags=_re.I)
                if r["reText"] != expect_re:
                    bad("replace", "a regex Replace all did not expand $1")
                if not r["undoRe"]:
                    bad("replace", "Ctrl+Z did not undo the regex Replace all")

            e = await js(ESC)
            if not e or e.get("error"):
                bad("esc-focus", f"Esc threw: {(e or {}).get('error')}")
            else:
                if e["bar"]:
                    bad("esc-focus", "Esc did not close the bar")
                if not e["focused"]:
                    bad("esc-focus", "Esc did not give focus back to the file")
                if e["sel"] != "NEEDLE_3":
                    bad("esc-focus", f"after Esc the file's selection is {e['sel']!r}, not the match")
                if e["marks"]:
                    bad("esc-focus", "match highlights stayed drawn after the bar closed")

            s = await js(SEARCH)
            if not s or s.get("error"):
                bad("search-in-files", f"search threw: {(s or {}).get('error')}")
            else:
                if not s["prevented"] or not s["panel"] or not s["focus"]:
                    bad("search-in-files", "Ctrl+Shift+H did not open and focus the Search panel")
                if s["files"] != ["sub/deep.py"] or len(s["hits"]) != 2:
                    bad("search-in-files", f"expected 2 hits in sub/deep.py, got {s['files']} {s['hits']}")
                if "logo.png" in s["fetched"] or "huge.log" in s["fetched"]:
                    bad("search-in-files", f"a binary or oversized file was read: {s['fetched']}")
                if "skipped" not in s["note"]:
                    bad("search-in-files", f"the summary does not say what was skipped: {s['note']!r}")
                if s["tab"] != "deep.py" or s["sel"] != "deepword" or s["line"] != 4:
                    bad("search-in-files", f"opening the hit gave tab {s['tab']!r}, selection {s['sel']!r} "
                                           f"on line {s['line']} — expected deep.py, 'deepword', line 4")
                if not s["focused"]:
                    bad("search-in-files", "the opened file did not get focus")

            await viewport(360, 740, True)
            await asyncio.sleep(0.3)
            p = await js(PHONE)
            if not p or p.get("error"):
                bad("phone-width", f"{(p or {}).get('error')}")
            else:
                if p["left"] < 0 or p["right"] > p["vw"] + 1 or p["off"]:
                    bad("phone-width", f"the bar spills off a 360px screen ({p['left']:.0f}..{p['right']:.0f}, "
                                       f"{p['off']} buttons off-screen)")
                if p["qw"] < 60 or p["rw"] < 60:
                    bad("phone-width", f"the inputs are too narrow to use ({p['qw']:.0f}/{p['rw']:.0f}px)")
                if p["sw"] > p["vw"] + 1:
                    bad("phone-width", f"the page scrolls sideways ({p['sw']}px > {p['vw']}px)")

            for theme in ("professional", "cyberpunk"):
                await js(f"document.documentElement.setAttribute('data-theme','{theme}')", awaited=False)
                await asyncio.sleep(0.2)
                t = await js(THEME, awaited=False)
                if not t or t.get("error"):
                    bad("theme", f"{theme}: {(t or {}).get('error')}")
                elif t["fg"] == t["bg"] or t["input"] == t["ibg"]:
                    bad("theme", f"{theme}: find bar text is invisible against its background")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for kind, msg in problems:
            print(f"  {kind}: {msg}")
        return 1
    print("OK  find / replace / search-in-files checks passed")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="codefind-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE.replace("__FILES__", json.dumps(FILES)))

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
    try:
        return asyncio.run(drive(f"http://127.0.0.1:{srv.server_port}/index.html"))
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
