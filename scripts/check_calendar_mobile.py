#!/usr/bin/env python3
"""Layout + behaviour check for the CALENDAR screen, at phone and desktop widths.

    venv-unified/bin/python scripts/check_calendar_mobile.py

check_client_mobile.py only ever loads the timeline, so it never opens this screen. This drives the
real calendar.js against a stubbed `window.__PC` and a stubbed /api/calendar/* — no server, no relay,
no login — and audits what a phone actually gets.

Assertions, each a way a month grid specifically breaks:

  horizontal-overflow  Seven columns at 360px. A grid that sizes cells in px instead of fractions
                       pushes the page sideways, which is the single most obvious "this is broken".
  grid-incomplete      Fewer than 42 day cells, or no weekday header: the month is drawn from a
                       computed start-of-grid, and an off-by-one there silently drops a week.
  wrong-day-count      The days shown for the month don't match the real calendar — the classic
                       Monday-first/Sunday-first mistake, which puts every event on the wrong column.
  event-not-on-its-day An event whose DTSTART is on the selected day does not appear in the day
                       panel. Parsing DTSTART is the one piece of iCalendar this screen must get
                       right; an all-day event stored as VALUE=DATE and read as UTC lands a day out
                       for anyone west of London.
  tiny-tap-target      A day cell or control under 32px — the grid is the primary control here.
  ios-zoom-trap        Any input under 16px: iOS zooms on focus and never zooms back.
  under-nav            The day panel's bottom sits under the fixed .mobilenav.
  state-lost           Leaving the view and coming back must keep the month you were on. #feed is
                       shared and blanked on entry, so a screen holding its state in the DOM loses it.
  missing-control      The month title, the navigation, or "＋ Event" did not render.

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / websockets).
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
WIDTHS = [(390, 844, True), (360, 780, True), (1280, 860, False)]
PORT = 9483
PROFILE = "/tmp/pc-calendar-check"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div class="app" style="display:flex;flex-direction:column;height:100dvh">
  <div id="feed" class="feed"></div>
</div>
<nav class="mobilenav glass"><button class="nav-item"><b>Home</b></button></nav>
<div id="modal-root"></div><div id="toast-root"></div>
<script src="/static/js/client/sprite.js"></script>
<script>
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.__view = 'calendar';
window.__toasts = [];
// Two events on a KNOWN day — one timed, one all-day — so the day panel can be checked against a
// date the test computes rather than against whatever the code happens to produce.
const D = new Date();
const pad = n => String(n).padStart(2,'0');
const TODAY = D.getFullYear()+'-'+pad(D.getMonth()+1)+'-'+pad(D.getDate());
const YMD = TODAY.replace(/-/g,'');
window.__today = TODAY;
const ITEMS = [
  { uid:'timed-1', cal:'work', component:'VEVENT',
    ics:'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:timed-1\r\nDTSTART:'+YMD+'T140000Z\r\nDTEND:'+YMD+'T150000Z\r\nSUMMARY:Dentist\r\nLOCATION:High Street\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n' },
  { uid:'allday-1', cal:'work', component:'VEVENT',
    ics:'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:allday-1\r\nDTSTART;VALUE=DATE:'+YMD+'\r\nDTEND;VALUE=DATE:'+YMD+'\r\nSUMMARY:Public holiday\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n' },
];
window.__loads = 0;
window.fetch = async (url, opts) => {
  const u = String(url);
  const j = d => ({ ok:true, status:200, json: async()=>d });
  if(u.startsWith('/api/calendar/config'))
    return j({ enabled:true, url:'https://node.example/caldav/me/', username:'me', has_password:false });
  if(u.startsWith('/api/calendar/calendars'))
    return j({ calendars:[{id:'work',displayname:'Work',color:'#3ce8ff'},
                          {id:'home',displayname:'Home',color:'#ff5cf0'}] });
  if(u.startsWith('/api/calendar/items')){ window.__loads++; return j({ items: u.includes('cal=work') ? ITEMS : [] }); }
  if(u.startsWith('/api/calendar/password')) return j({ password:'aaa-bbb-ccc' });
  return j({ ok:true });
};
window.__PC = {
  $, $$, enc,
  toast: m => { window.__toasts.push(m); },
  modal: (html, onMount) => { const bg=document.createElement('div'); bg.className='modal-bg';
    document.body.classList.add('modal-open');
    bg.innerHTML = '<div class="modal glass neon-border">'+html+'</div>';
    $('#modal-root').appendChild(bg); if(onMount) onMount(bg.querySelector('.modal')); },
  closeModal: () => { const m=$('#modal-root .modal-bg'); if(m) m.remove();
                      document.body.classList.remove('modal-open'); },
  authFetch: (u,o) => window.fetch(u,o),
  ensureAiSession: async () => ({ can_ai:true }),
  uiConfirm: async () => true,
  switchView: v => { window.__view = v; },
  get ME(){ return {pubkey:'me'}; },
  get VIEW(){ return window.__view; },
};
</script>
<script src="/static/js/client/calendar.js"></script>
<script>
(async function(){
  for(let i=0;i<80 && !window.PCCalendar;i++) await new Promise(r=>setTimeout(r,50));
  window.PCCalendar.render();
  for(let i=0;i<80 && !document.querySelector('.cal-day');i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,400));
  window.__ready = true;
})();
</script>
</body></html>"""

AUDIT = r"""(() => {
  const out = {overflow:false, days:0, dow:0, title:'', hasNew:false, small:[], zoomy:[],
               panelBottom:0, navTop:0, evTitles:[], evMeta:[], monthDays:0, firstCol:''};
  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  const vis = el => el && (!el.checkVisibility || el.checkVisibility());
  out.days = document.querySelectorAll('.cal-day').length;
  out.dow = document.querySelectorAll('.cal-dow').length;
  out.firstCol = (document.querySelector('.cal-dow') || {}).textContent || '';
  out.title = (document.querySelector('.cal-title') || {}).textContent || '';
  out.hasNew = !!document.querySelector('#cal-new');
  // Cells belonging to the shown month (not greyed) — must equal the real length of that month.
  out.monthDays = document.querySelectorAll('.cal-day:not(.other)').length;
  out.evTitles = [...document.querySelectorAll('.cal-evtitle')].map(e => e.textContent.trim());
  out.evMeta = [...document.querySelectorAll('.cal-evmeta')].map(e => e.textContent.trim());
  document.querySelectorAll('.cal-day, .cal-nav .btn, .cal-tools .btn, .cal-ev .btn').forEach(b => {
    if (!vis(b)) return;
    const r = b.getBoundingClientRect();
    if (r.height < 32) out.small.push({cls: String(b.className).slice(0,22), h: Math.round(r.height)});
  });
  // TEXT fields only: iOS zooms when the caret lands in one. A checkbox, radio or button has no
  // caret and never triggers it, so flagging those is noise that hides the real ones.
  const TEXTY = ['text','search','email','url','tel','number','password','date','time',
                 'datetime-local','month','week',''];
  document.querySelectorAll('input, select, textarea').forEach(i => {
    if (!vis(i)) return;
    if (i.tagName === 'INPUT' && !TEXTY.includes((i.type||'').toLowerCase())) return;
    const fs = parseFloat(getComputedStyle(i).fontSize);
    if (fs < 16) out.zoomy.push({cls: String(i.className).slice(0,22) || i.type, fs});
  });
  const panel = document.querySelector('.cal-day-panel');
  const nav = document.querySelector('.mobilenav');
  out.panelBottom = panel ? Math.round(panel.getBoundingClientRect().bottom) : 0;
  out.navTop = (nav && vis(nav)) ? Math.round(nav.getBoundingClientRect().top) : window.innerHeight;
  return out;
})()"""

NEXT_MONTH_AND_BACK = r"""(async () => {
  const title0 = document.querySelector('.cal-title').textContent;
  document.querySelector('#cal-next').click();
  await new Promise(r=>setTimeout(r,250));
  const title1 = document.querySelector('.cal-title').textContent;
  // Leave the view the way app.js does, then come back.
  const feed = document.getElementById('feed');
  window.__view = 'home';
  feed.innerHTML = '<div class="spinner"></div>';
  await new Promise(r=>setTimeout(r,120));
  window.__view = 'calendar';
  window.PCCalendar.render();
  for (let i=0;i<40 && !document.querySelector('.cal-day'); i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,250));
  return { title0, title1, back: document.querySelector('.cal-title').textContent };
})()"""

OPEN_EDITOR = r"""(async () => {
  document.querySelector('#cal-new').click();
  await new Promise(r=>setTimeout(r,250));
  const m = document.querySelector('#modal-root .modal');
  if (!m) return {error:'no editor opened'};
  const inputs = [...m.querySelectorAll('input, textarea, select')];
  // Same rule as the page audit: only fields with a CARET make iOS zoom. A checkbox does not.
  const TEXTY = ['text','search','email','url','tel','number','password','date','time',
                 'datetime-local','month','week',''];
  const small = inputs.filter(i => !(i.tagName === 'INPUT' && !TEXTY.includes((i.type||'').toLowerCase())))
                      .filter(i => parseFloat(getComputedStyle(i).fontSize) < 16)
                      .map(i => (i.id || i.type) + ':' + getComputedStyle(i).fontSize);
  const r = m.getBoundingClientRect();
  return { fields: inputs.length, small,
           wide: Math.round(r.width) > window.innerWidth + 1,
           hasDate: !!m.querySelector('#cev-date'), hasSave: !!m.querySelector('#cev-save') };
})()"""


async def drive(url):
    import websockets  # noqa: F811
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
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

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:300])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            import datetime
            today = datetime.date.today()
            nxt = datetime.date(today.year + (today.month == 12), (today.month % 12) + 1, 1)
            days_in_month = (nxt - datetime.date(today.year, today.month, 1)).days

            for w, h, phone in WIDTHS:
                label = f"{w}px"
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2 if phone else 1, "mobile": phone})
                await call("Emulation.setTouchEmulationEnabled",
                           {"enabled": phone, "maxTouchPoints": 5 if phone else 0})
                await call("Page.navigate", {"url": url})
                ready = False
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        ready = True
                        break
                if not ready:
                    print(f"SKIP  {label}: the calendar never finished rendering")
                    return 2

                r = await js(AUDIT)
                if r is None:
                    print(f"SKIP  {label}: page did not evaluate")
                    return 2
                if r["overflow"]:
                    problems.append((label, "horizontal-overflow", "the month grid scrolls the page sideways"))
                if r["days"] != 42 or r["dow"] != 7:
                    problems.append((label, "grid-incomplete",
                                     f"{r['days']} day cells and {r['dow']} weekday headers (want 42 and 7)"))
                if r["monthDays"] != days_in_month:
                    problems.append((label, "wrong-day-count",
                                     f"{r['monthDays']} cells for this month, but it has {days_in_month} days"))
                if r["firstCol"].strip() != "Mon":
                    problems.append((label, "wrong-day-count",
                                     f"the grid starts on {r['firstCol']!r} — the cells are computed Monday-first"))
                if not r["title"] or not r["hasNew"]:
                    problems.append((label, "missing-control", "no month title or no ＋ Event button"))
                # Both of today's events must be on today.
                if "Dentist" not in r["evTitles"] or "Public holiday" not in r["evTitles"]:
                    problems.append((label, "event-not-on-its-day",
                                     f"the day panel shows {r['evTitles']!r}"))
                if not any("All day" in m for m in r["evMeta"]):
                    problems.append((label, "event-not-on-its-day",
                                     "the all-day event is not marked as all day (VALUE=DATE misread?)"))
                if phone:
                    for t in r["small"]:
                        problems.append((label, "tiny-tap-target", f"{t['cls']} is {t['h']}px tall"))
                    for z in r["zoomy"]:
                        problems.append((label, "ios-zoom-trap", f"{z['cls']} is {z['fs']}px"))
                    if r["panelBottom"] > r["navTop"] + 1:
                        problems.append((label, "under-nav",
                                         f"the day panel's bottom ({r['panelBottom']}px) is under the nav "
                                         f"({r['navTop']}px)"))

                st = await js(NEXT_MONTH_AND_BACK, awaited=True)
                if not st:
                    problems.append((label, "state-lost", "could not run the month-navigation test"))
                else:
                    if st["title0"] == st["title1"]:
                        problems.append((label, "missing-control", "› did not change the month"))
                    elif st["back"] != st["title1"]:
                        problems.append((label, "state-lost",
                                         f"came back on {st['back']!r} instead of {st['title1']!r}"))

                ed = await js(OPEN_EDITOR, awaited=True)
                if not ed or ed.get("error"):
                    problems.append((label, "missing-control", f"the event editor did not open ({(ed or {}).get('error')})"))
                else:
                    if not ed["hasDate"] or not ed["hasSave"]:
                        problems.append((label, "missing-control", "the editor has no day field or no Save"))
                    if ed["wide"]:
                        problems.append((label, "horizontal-overflow", "the editor is wider than the screen"))
                    if phone:
                        for s in ed["small"]:
                            problems.append((label, "ios-zoom-trap", f"editor field {s}"))
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for label, kind, msg in problems:
            print(f"  [{label}] {kind}: {msg}")
        return 1
    print("OK  calendar mobile checks passed")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="calcheck-")
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
    url = f"http://127.0.0.1:{srv.server_port}/index.html"
    try:
        return asyncio.run(drive(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
