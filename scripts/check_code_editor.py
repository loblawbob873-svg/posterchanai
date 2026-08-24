#!/usr/bin/env python3
"""Behaviour check for POSTERCHAN CODE, in a real browser.

    venv-unified/bin/python scripts/check_code_editor.py

check_client_mobile.py never opens this screen. This drives the real code.js against a stubbed
`window.__PC` and a stubbed /api/code/* (no server, no login, no node), and measures the things that
only a browser can answer.

Assertions, each a way THIS screen breaks:

  state-lost-on-repaint   THE requirement the feature is judged on. Three separate things repaint
                          this view — #feed is shared and blanked on entry, refocusing a desktop
                          window re-renders it, and a resize can do both — and every one of them
                          destroys the DOM. After a repaint the buffer, the CARET and the SCROLL
                          must all be exactly where they were.
  state-lost-on-reload    THE HARD HALF, and the one that cannot be noticed on one screen. A MONITOR
                          HANDOFF REBUILDS THE WINDOW IN A DIFFERENT ELECTRON RENDERER — os.js says
                          "no DOM node can literally cross between" monitors — which is a different
                          JavaScript context with an empty module state. Modelled here by a real page
                          reload against the same origin: only the localStorage mirror can survive
                          it, and unsaved work must come back with it.
  layers-misaligned       The editor is a textarea over a highlighted <pre>. If the two disagree
                          about ANY metric that positions a glyph — font, size, line-height,
                          letter-spacing, tab-size, padding, white-space — the colours slide off the
                          letters progressively: fine on line 1, a mess by line 60. Measured as
                          computed style, because it looks correct in a screenshot of the top.
  tab-leaves-the-field    In a plain textarea, Tab moves focus to the next control — so the caret
                          leaves the file every time somebody indents. Must insert indentation and
                          keep focus.
  no-colour               A token class rendered but resolves to no colour, or to the same colour as
                          plain text. Checked in a LIGHT theme too: every colour here comes from a
                          palette variable, and a theme that redefines the palette must not leave a
                          token invisible against its own background.
  horizontal-overflow     The page scrolls sideways. Code lines are long and unbreakable by nature,
                          so the editor must scroll INSIDE its own box.
  missing-control         The tree, the tabs, the toolbar or the editor did not render at all.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / websockets).
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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9486)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-code-editor-check"

# Long enough to scroll, and containing every construct the highlighter has an opinion about.
SAMPLE = "\n".join(
    ['"""A module docstring."""', "import os", "", "", "def handler(name, count=3):",
     "    # a comment with an apostrophe: it's fine", "    total = 0xFF + 1.5e3",
     "    label = 'a string with def and # inside'", "    for i in range(count):",
     "        total += i", "    return {'name': name, 'total': total}", ""]
    + ["    # filler line %d, long enough that the editor must scroll vertically" % i
       for i in range(60)]
)

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
window.__SAMPLE = __SAMPLE__;
window.__saved = null;
// The node, stubbed. This tests the screen, not the server.
window.fetch = async (url, opts) => {
  const u = String(url); const j = d => ({ ok:true, status:200, json: async()=>d });
  if(u.startsWith('/api/code/config'))
    return j({ root:'/srv/workspace', engines:{python:'black', bash:'beautysh'}, maxBytes:2097152 });
  if(u.startsWith('/api/code/tree'))
    return j({ path:'', truncated:false, entries:[
      {name:'app', dir:true, size:0, mtime:1, lang:''},
      {name:'handler.py', dir:false, size:900, mtime:2, lang:'python'},
      {name:'run.sh', dir:false, size:80, mtime:3, lang:'bash'}]});
  if(u.startsWith('/api/code/file') && (!opts || opts.method !== 'POST'))
    return j({ path:'handler.py', text: window.__SAMPLE, lang:'python', size:900, mtime:2 });
  if(u.startsWith('/api/code/file'))
    { window.__saved = JSON.parse(opts.body); return j({ ok:true, path:'handler.py', bytes:1, mtime:9 }); }
  if(u.startsWith('/api/code/format'))
    return j({ ok:true, source: JSON.parse(opts.body).source, engine:'black', changed:false });
  return j({});
};
window.__PC = {
  $, $$, enc,
  toast: m => {},
  authFetch: (u,o) => window.fetch(u,o),
  ensureAiSession: async () => ({ can_ai:true, is_admin:true }),
  switchView: v => { window.__view = v; },
  get ME(){ return {pubkey:'abcdef012345'}; },
  get VIEW(){ return window.__view; },
};
</script>
<script src="/static/js/client/code.js"></script>
<script>
(async function(){
  for(let i=0;i<80 && !window.PCCode;i++) await new Promise(r=>setTimeout(r,50));
  await window.PCCode.render();
  window.__ready = true;
})();
</script>
</body></html>"""

OPEN_FILE = r"""(async () => {
  const t = [...document.querySelectorAll('[data-file]')].find(b => b.dataset.file === 'handler.py');
  if(!t) return {error:'no handler.py in the tree'};
  t.click();
  for(let i=0;i<60 && !document.querySelector('#pcc-ta');i++) await new Promise(r=>setTimeout(r,50));
  const ta = document.querySelector('#pcc-ta');
  return { opened: !!ta, len: ta ? ta.value.length : 0 };
})()"""

# Type, move the caret into the middle, scroll down. This is "where I was".
EDIT = r"""(async () => {
  const ta = document.querySelector('#pcc-ta');
  if(!ta) return {error:'no textarea'};
  ta.focus();
  ta.setSelectionRange(0,0);
  // A real insertion through the input path the module listens to.
  ta.value = 'CANARY = 1\n' + ta.value;
  ta.dispatchEvent(new Event('input', {bubbles:true}));
  ta.scrollTop = 220;
  ta.dispatchEvent(new Event('scroll', {bubbles:true}));
  ta.setSelectionRange(140, 146);
  ta.dispatchEvent(new Event('keyup', {bubbles:true}));
  await new Promise(r=>setTimeout(r,120));
  return { text: ta.value.slice(0,12), sel: [ta.selectionStart, ta.selectionEnd],
           scroll: Math.round(ta.scrollTop) };
})()"""

REPAINT = r"""(async () => {
  // What a refocus or a resize-driven re-render does: #feed is blanked and render() runs again.
  document.querySelector('#feed').innerHTML = '';
  await window.PCCode.render();
  await new Promise(r=>setTimeout(r,150));
  const ta = document.querySelector('#pcc-ta');
  if(!ta) return {error:'the editor did not come back at all'};
  return { text: ta.value.slice(0,12), sel: [ta.selectionStart, ta.selectionEnd],
           scroll: Math.round(ta.scrollTop), len: ta.value.length };
})()"""

AFTER_RELOAD = r"""(async () => {
  for(let i=0;i<100 && !window.__ready;i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,300));
  const ta = document.querySelector('#pcc-ta');
  if(!ta) return {error:'no editor after reload'};
  return { text: ta.value.slice(0,12), sel: [ta.selectionStart, ta.selectionEnd],
           scroll: Math.round(ta.scrollTop), len: ta.value.length,
           tabs: document.querySelectorAll('.pcc-tab').length };
})()"""

TAB_KEY = r"""(async () => {
  const ta = document.querySelector('#pcc-ta');
  if(!ta) return {error:'no textarea'};
  ta.focus();
  ta.setSelectionRange(0,0);
  const before = ta.value;
  const ev = new KeyboardEvent('keydown', {key:'Tab', bubbles:true, cancelable:true});
  ta.dispatchEvent(ev);
  await new Promise(r=>setTimeout(r,80));
  return { prevented: ev.defaultPrevented, grew: ta.value.length > before.length,
           startsWithSpace: /^[ \t]/.test(ta.value),
           stillFocused: document.activeElement === ta };
})()"""

METRICS = r"""(() => {
  const ta = document.querySelector('#pcc-ta');
  const hl = document.querySelector('#pcc-hl');
  if(!ta || !hl) return {error:'layers missing'};
  const keys = ['fontFamily','fontSize','fontWeight','lineHeight','letterSpacing','tabSize',
                'paddingTop','paddingLeft','paddingRight','paddingBottom','whiteSpace',
                'wordBreak','overflowWrap','borderTopWidth','borderLeftWidth','textTransform'];
  const a = getComputedStyle(ta), b = getComputedStyle(hl);
  const diff = keys.filter(k => String(a[k]) !== String(b[k])).map(k => k+': ta='+a[k]+' hl='+b[k]);
  return { diff, overflow: document.documentElement.scrollWidth > window.innerWidth + 1 };
})()"""

COLOURS = r"""(() => {
  const hl = document.querySelector('#pcc-hl');
  if(!hl) return {error:'no highlight layer'};
  const plain = getComputedStyle(hl).color;
  const seen = {};
  for(const cls of ['t-kw','t-str','t-com','t-num','t-fn']){
    const el = hl.querySelector('.'+cls);
    seen[cls] = el ? getComputedStyle(el).color : '';
  }
  const bg = getComputedStyle(document.querySelector('.pcc-editwrap') || hl).backgroundColor;
  return { plain, seen, bg,
           tree: document.querySelectorAll('.pcc-item').length,
           tabs: document.querySelectorAll('.pcc-tab').length,
           toolbar: !!document.querySelector('#pcc-save'),
           gutter: !!document.querySelector('#pcc-gutter') };
})()"""


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
    page = None
    try:
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

        problems = []
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print(json.dumps(r["exceptionDetails"])[:1200])
                    return None
                return (r.get("result") or {}).get("value")

            async def goto(u, theme=""):
                await call("Page.navigate", {"url": u + ("#" + theme if theme else "")})
                for _ in range(120):
                    if await js("!!window.__ready"):
                        return True
                    await asyncio.sleep(0.25)
                return False

            await call("Page.enable")
            await call("Runtime.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": 1280, "height": 860, "deviceScaleFactor": 1, "mobile": False})

            if not await goto(url):
                print("SKIP  the page never became ready")
                return 2

            got = await js(OPEN_FILE, awaited=True)
            if not got or got.get("error") or not got.get("opened"):
                problems.append(("missing-control",
                                 f"could not open a file ({(got or {}).get('error')})"))
                print("FAIL  " + problems[0][1])
                return 1

            base = await js(COLOURS)
            if not base or base.get("error"):
                problems.append(("missing-control", "the editor chrome did not render"))
            else:
                if not base["tree"]:
                    problems.append(("missing-control", "the file tree rendered no entries"))
                if not base["tabs"]:
                    problems.append(("missing-control", "no tab for the open file"))
                if not base["toolbar"]:
                    problems.append(("missing-control", "no toolbar"))
                if not base["gutter"]:
                    problems.append(("missing-control", "no line-number gutter"))
                for cls, col in base["seen"].items():
                    if not col:
                        problems.append(("no-colour", f"no {cls} token rendered at all"))
                    elif col == base["plain"]:
                        problems.append(("no-colour",
                                         f"{cls} resolves to the same colour as plain text ({col})"))
                    elif col == base["bg"]:
                        problems.append(("no-colour", f"{cls} is invisible against its background"))

            m = await js(METRICS)
            if not m or m.get("error"):
                problems.append(("layers-misaligned", "could not measure the two layers"))
            else:
                for d in m["diff"]:
                    problems.append(("layers-misaligned", d))
                if m["overflow"]:
                    problems.append(("horizontal-overflow", "the page scrolls sideways"))

            tk = await js(TAB_KEY, awaited=True)
            if not tk or tk.get("error"):
                problems.append(("tab-leaves-the-field", "could not press Tab"))
            else:
                if not tk["prevented"]:
                    problems.append(("tab-leaves-the-field",
                                     "Tab was not intercepted — focus leaves the file"))
                if not (tk["grew"] and tk["startsWithSpace"]):
                    problems.append(("tab-leaves-the-field", "Tab inserted no indentation"))
                if not tk["stillFocused"]:
                    problems.append(("tab-leaves-the-field", "focus left the textarea"))

            before = await js(EDIT, awaited=True)
            if not before or before.get("error"):
                problems.append(("state-lost-on-repaint", "could not edit the buffer"))
            else:
                after = await js(REPAINT, awaited=True)
                if not after or after.get("error"):
                    problems.append(("state-lost-on-repaint",
                                     f"the editor did not survive a repaint "
                                     f"({(after or {}).get('error')})"))
                else:
                    if after["text"] != before["text"]:
                        problems.append(("state-lost-on-repaint",
                                         f"the unsaved buffer changed across a repaint "
                                         f"({after['text']!r} was {before['text']!r})"))
                    if after["sel"] != before["sel"]:
                        problems.append(("state-lost-on-repaint",
                                         f"the caret moved across a repaint "
                                         f"({after['sel']} was {before['sel']})"))
                    if abs(after["scroll"] - before["scroll"]) > 8:
                        problems.append(("state-lost-on-repaint",
                                         f"the scroll moved across a repaint "
                                         f"({after['scroll']}px was {before['scroll']}px)"))

                # THE MONITOR HANDOFF. A real reload: a new JavaScript context, empty module state,
                # same origin. Only the localStorage mirror can carry the session across it.
                if not await goto(url):
                    problems.append(("state-lost-on-reload", "the page never came back"))
                else:
                    rl = await js(AFTER_RELOAD, awaited=True)
                    if not rl or rl.get("error"):
                        problems.append(("state-lost-on-reload",
                                         f"nothing was restored ({(rl or {}).get('error')})"))
                    else:
                        if not rl["tabs"]:
                            problems.append(("state-lost-on-reload",
                                             "the open file was not reopened in a new renderer"))
                        if rl["text"] != before["text"]:
                            problems.append(("state-lost-on-reload",
                                             f"unsaved work did not cross renderers "
                                             f"({rl['text']!r} was {before['text']!r})"))
                        if rl["sel"] != before["sel"]:
                            problems.append(("state-lost-on-reload",
                                             f"the caret did not cross renderers "
                                             f"({rl['sel']} was {before['sel']})"))

            # A LIGHT THEME. Every colour here comes from a palette variable; a theme that redefines
            # the palette must not leave a token unreadable against its own background.
            await js("document.documentElement.setAttribute('data-theme','professional')")
            await asyncio.sleep(0.3)
            lt = await js(COLOURS)
            if lt and not lt.get("error"):
                for cls, col in lt["seen"].items():
                    if col and col == lt["bg"]:
                        problems.append(("no-colour",
                                         f"{cls} is invisible in the professional (light) theme"))
                if lt["plain"] == base.get("plain"):
                    problems.append(("no-colour",
                                     "the light theme did not change the editor's text colour — "
                                     "a hard-coded colour somewhere in this block"))

        if problems:
            print(f"FAIL  {len(problems)} problem(s):")
            for kind, msg in problems:
                print(f"  {kind}: {msg}")
            return 1
        print("OK  PosterChan Code checks passed")
        return 0
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="codecheck-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE.replace("__SAMPLE__", json.dumps(SAMPLE)))

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
        return asyncio.run(drive(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
