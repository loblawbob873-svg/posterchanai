#!/usr/bin/env python3
"""Capture what the window ✨ panel sends for real PosterChan apps -> tests/fixtures/window_ai/<name>.json.

scripts/eval_window_ai.py scores the node's own model against these: a fixture is EXACTLY the request the
shipped client sends when somebody presses Ask in that window (its visible text and numbered controls), so
the model is judged on what a person's window really looks like, not on a hand-written description of it.

Each scenario opens one app in the bundled client (the same offline harness the full-app tests use), may
click its way into a state first ("New event" -> the event form), then presses Ask and saves the request.

    venv-unified/bin/python scripts/capture_window_ai_fixtures.py            # every scenario
    venv-unified/bin/python scripts/capture_window_ai_fixtures.py calendar   # names containing "calendar"
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.client import test_desktop_offline_full_app as desktop  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "window_ai"

# name, view, buttons to press first (regexes over a control's visible text), extra setup JS
SCENARIOS = [
    ("calendar", "calendar", [], ""),
    ("calendar-form", "calendar", [r"^\+?\s*(new )?event$|add event"], ""),
    ("contacts", "contacts", [], ""),
    ("contacts-form", "contacts", [r"^\+?\s*(new )?contact$|add contact"], ""),
    ("websearch", "websearch", [], ""),
    ("torrents", "torrents", [], ""),
    ("notes-editor", "notes", [r"^new note$"], ""),
    ("budget", "budget", [], ""),
    ("budget-form", "budget", [r"add bill|new bill|^\+\s*bill"], ""),
    ("calculator", "calculator", [], ""),
]

CAPTURE = r"""(async(view, clicks)=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  window.__cap=null; const real=window.fetch;
  window.fetch=(u,o)=>{ if(String(u).includes('/api/chat-assist')){ try{ window.__cap=JSON.parse(o.body); }catch(_){}
      return Promise.resolve(new Response(JSON.stringify({ok:true,answer:'',tasks:[],steps:[]}),{status:200,headers:{'Content-Type':'application/json'}})); }
    return real(u,o); };
  __PC.switchView(view); await sleep(1500);
  for(const re of clicks){
    const rx=new RegExp(re,'i'), feed=document.getElementById('feed');
    let b=null;
    for(let i=0;i<40 && !b;i++){ b=[...feed.querySelectorAll('button,a,[role=button]')].find(x=>x.getClientRects().length && rx.test((x.getAttribute('aria-label')||x.textContent||'').trim())); if(!b) await sleep(200); }
    if(!b) return {error:'no control matching '+re, labels:[...feed.querySelectorAll('button')].map(x=>(x.getAttribute('aria-label')||x.textContent||'').trim()).filter(Boolean).slice(0,40)};
    b.click(); await sleep(1200);
  }
  PCOSWin.isWindow=()=>true; PCOSWin.viewOf=()=>view;
  const btn=document.createElement('button'); document.body.appendChild(btn); PCOS.pageWindowAI(btn,{});
  await sleep(300);
  const ta=document.querySelector('.osw-ai-panel textarea'); if(!ta) return {error:'no panel'};
  ta.value='x'; document.querySelector('.osw-ai-panel [data-ai-ask]').click();
  for(let i=0;i<30 && !window.__cap;i++) await sleep(100);
  return window.__cap || {error:'nothing sent'};
})"""


async def capture(name, view, clicks, setup):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 860, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        if setup:
            await b.js(setup)
        got["r"] = await b.js(CAPTURE + "(%s,%s)" % (json.dumps(view), json.dumps(clicks)))

    await desktop.with_browser("online", "", check)
    return got.get("r")


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    gen = desktop.bundle.__wrapped__(); next(gen)
    bad = 0
    try:
        for name, view, clicks, setup in SCENARIOS:
            if want and want not in name:
                continue
            r = asyncio.run(capture(name, view, clicks, setup))
            if not r or r.get("error"):
                bad += 1
                print(f"{name:16} FAILED  {json.dumps(r)[:400]}")
                continue
            r["instruction"] = "x"
            (OUT / f"{name}.json").write_text(json.dumps(r, indent=1, ensure_ascii=False) + "\n")
            print(f"{name:16} ok  {len(r.get('controls', []))} controls: "
                  + ", ".join(c['label'][:24] for c in r.get('controls', [])[:14]))
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
