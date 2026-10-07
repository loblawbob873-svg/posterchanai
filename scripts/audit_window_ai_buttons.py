#!/usr/bin/env python3
"""Press EVERY window ✨ button in EVERY PosterChan window, against THIS NODE'S OWN MODEL, and say what happened.

"Agentic window features useless in notes. You need to check all the windows and make sure agentic features
actually do something." A unit test proves a button sends the right request; it cannot say whether pressing it
leaves the person with something they can use. So this drives the SHIPPED client (the offline full-app
harness), opens ✨ in each app, presses each button in turn, and answers the request with the app's own
service code (chat_assist_service) talking to the node's model over /v1 -- the same prompts, parsing and
validation production runs. After the answer it presses the answer's first ACTION (Replace the note, Use this,
Do all, Add to Calendar…) and records what changed on screen. One line per button:

    <window>  <button>  -> <what came back>  => <what the action did>

and a verdict: USEFUL (an answer AND an action that changed something), ANSWER-ONLY (text with nothing to do),
EMPTY / ERROR. Starters (buttons that only put a sentence in the ask box) are listed, not run.

    venv-unified/bin/python scripts/audit_window_ai_buttons.py               # every window
    venv-unified/bin/python scripts/audit_window_ai_buttons.py notes,global  # names containing these
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from tests.client import test_desktop_offline_full_app as desktop  # noqa: E402
from tests.client.test_dm_newest_first_full_app import RELAY  # noqa: E402
import capture_window_ai_fixtures as cap  # noqa: E402
from app.services import chat_assist_service as svc  # noqa: E402

BASE = "http://127.0.0.1:3051"
MODEL = "Qwen3.5-9B-abliterated-Q4_K_M.gguf"


def ask(messages, temperature=0.2):
    body = json.dumps({"model": MODEL, "messages": messages, "temperature": temperature, "max_tokens": 1200}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.load(r)["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


async def _chat(db, user, msgs, temperature):
    return await asyncio.to_thread(ask, msgs, temperature)


svc._chat = _chat


async def dispatch(b):
    """chat_assist.py's dispatch, minus auth: the same service calls with the same fields."""
    a = b.get("action")
    w = b.get("windows") or []
    if a == "window_steps":
        return await svc.window_steps(None, None, w, b.get("instruction", ""), b.get("history") or [],
                                      b.get("commands", False), b.get("today", ""), b.get("controls") or [])
    if a == "window_recipe":
        return await svc.window_recipe(None, None, w, b.get("recipe", ""), b.get("today", ""), b.get("text", ""),
                                       b.get("instruction", ""), b.get("reply_ref"), b.get("box_ref"), b.get("box_label", ""))
    if a == "window_feed":
        return await svc.window_feed(None, None, b.get("posts") or [], b.get("recipe", ""), b.get("instruction", ""),
                                     b.get("target", 0), b.get("subject", "posts"))
    if a == "window_note":
        return await svc.window_note(None, None, b.get("recipe", ""), b.get("title", ""), b.get("text", ""),
                                     b.get("instruction", ""))
    if a == "window_calc":
        return await svc.window_calc(None, None, b.get("instruction", ""))
    if a == "window_contact":
        return await svc.window_contact(None, None, b.get("instruction", ""))
    if a == "window_event":
        return {"event": await svc.window_event(None, None, w, b.get("answer", ""), b.get("today", ""))}
    if a == "window":
        return {"answer": await svc.ask_window(None, None, w, b.get("instruction", ""))}
    raise svc.AssistError(400, "audit: unknown action " + str(a))


CALLS = []


# ---------------------------------------------------------------- what the windows hold
PEOPLE = [(2, "Alice"), (3, "Bob"), (4, "Carol"), (5, "Dana"), (6, "Eve")]
POSTS = [  # (author sk, text, seconds ago, addressed to me)
    (2, "Anyone know a good Wayland compositor for an old laptop? Sway feels heavy on my ThinkPad X220.", 300, False),
    (3, "@you did you ever get the Arc A770 running llama.cpp with SYCL? Would love your notes.", 600, True),
    (4, "Bitcoin fees are down to 2 sat/vB again, good time to consolidate UTXOs.", 900, False),
    (5, "Gentoo meetup this Saturday 3pm at the Brewhouse downtown — bring a laptop if you want help with installs.", 1200, False),
    (6, "Just switched my whole family to self-hosted Nextcloud. Calendar sync on iOS was the hardest part.", 1500, False),
    (2, "Kernel 6.18 broke my wifi (iwlwifi) — rolled back to 6.17 and it works again.", 1800, False),
    (3, "Monero dev meeting notes are up, mostly about FCMP++ timing.", 2400, False),
    (4, "@you thanks for the relay recommendation yesterday, poster.place has been rock solid.", 3000, True),
    (5, "Hot take: tiling window managers are a productivity trap.", 3600, False),
    (6, "Can someone review my PR for the NIP-46 signer? Link in my profile. Need it merged by Friday.", 4200, False),
]
SEED_FEED = r"""(()=>{const me=NostrTools.getPublicKey(new Uint8Array(32).fill(1)), now=Math.floor(Date.now()/1000), r=_rel();
  const people=%s, posts=%s;
  for(const [sk,name] of people) r.push(NostrTools.finalizeEvent({kind:0,created_at:now-86400,tags:[],content:JSON.stringify({name})},new Uint8Array(32).fill(sk)));
  for(const [sk,text,ago,toMe] of posts) r.push(NostrTools.finalizeEvent({kind:1,created_at:now-ago,tags:toMe?[['p',me]]:[],content:text.replace('@you ','')},new Uint8Array(32).fill(sk)));
  r.push(NostrTools.finalizeEvent({kind:3,created_at:now-86400,tags:people.map(([sk])=>['p',NostrTools.getPublicKey(new Uint8Array(32).fill(sk))]),content:''},new Uint8Array(32).fill(1)));
  localStorage.setItem('__relayEvents',JSON.stringify(r));})()""" % (json.dumps(PEOPLE), json.dumps(POSTS))

NOTES = [("Home wifi", "network: casa-5g\npassword: hunter2-blue\nrouter admin at 192.168.0.1"),
         ("Groceries", "milk, eggs, tortillas, cilantro, limes, chicken thighs"),
         ("Trip ideas", "lisbon in spring? or porto. need to check flights. maybe 5 days. ask dana if she wants to come"),
         ("Server todo", "renew the tls cert before the 20th\nupgrade postgres 15 -> 17\nmove backups to the nas")]
SEED_NOTES = r"""(async()=>{ for(let i=0;i<60 && !(window.PCNotes&&PCNotes.save);i++) await new Promise(r=>setTimeout(r,200));
  const ids=[]; for(const [title,body] of %s){ const r=await PCNotes.save({title,body}); ids.push(r.id); }
  window.__noteIds=ids; return ids.length; })()""" % json.dumps(NOTES)


def open_note(i):
    return "js:(async()=>{ await PCNotes.select(window.__noteIds[%d]); })()" % i


# name, view, clicks (as capture_window_ai_fixtures), setup JS run before the view opens
SCENARIOS = [
    ("global-feed", "global", [], SEED_FEED),
    ("home-feed", "home", [], SEED_FEED),
    ("notifications", "notifications", [], SEED_FEED),
    ("notes-list", "notes", ["js:" + SEED_NOTES], ""),
    ("notes-open", "notes", ["js:" + SEED_NOTES, open_note(2)], ""),
    ("notes-untitled", "notes", ["js:" + SEED_NOTES, "js:(async()=>{const r=await PCNotes.save({title:'',body:'call the dentist monday, pick up dry cleaning, email landlord about the leak'});await PCNotes.select(r.id);})()"], ""),
    ("notes-new-empty", "notes", [r"^new note$"], ""),
] + [s for s in cap.SCENARIOS if not s[0].startswith("notes")]
QUERIES = {"calc": "15% tip on a $84.50 dinner split between 3 people", "contact": "Bob Smith from Acme, 555-123-4567, bob@acme.test", "find": "linux", "notes-find": "wifi password", "write": "packing list for a beach weekend"}

# The page never reaches the network for the AI: its request is parked in window.__aiq and answered from
# Python (the app's service code + the node's model), so no page call has to outlive the harness's 60 s
# CDP budget and nothing depends on the page being allowed to talk to another origin.
PREP = r"""(async(view, clicks)=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  window.__aiq=[]; window.__aians={}; let seq=0;
  const parked=(u,o)=>new Promise(res=>{ const id=++seq; __aiq.push({id,body:JSON.parse(o.body)});
    const t=setInterval(()=>{ const a=__aians[id]; if(a){ clearInterval(t); delete __aians[id];
      res(new Response(JSON.stringify(a.body),{status:a.status,headers:{'Content-Type':'application/json'}})); } },100); });
  const rf=window.fetch; window.fetch=(u,o)=>String(u).includes('/api/chat-assist')?parked(u,o):rf(u,o);
  try{ const af=__PC.authFetch; __PC.authFetch=(u,o)=>String(u).includes('/api/chat-assist')?parked(u,o):af(u,o); __PC.ensureAiSession=async()=>true; }catch(_){ }
  __PC.switchView(view); await sleep(2000);
  const root=()=>{ const d=[...document.querySelectorAll('.modal-bg')].filter(m=>m.getClientRects().length).pop(); return d?(d.querySelector('.modal')||d):document.getElementById('feed'); };
  for(const re of clicks){
    if(re.startsWith('js:')){ await (0,eval)(re.slice(3)); await sleep(1500); continue; }
    const rx=new RegExp(re,'i'); let b=null;
    for(let i=0;i<40 && !b;i++){ b=[...root().querySelectorAll('button,a,[role=button],[role=tab]')].find(x=>x.getClientRects().length && rx.test((x.getAttribute('aria-label')||x.textContent||'').trim())); if(!b) await sleep(200); }
    if(!b) return {error:'no control matching '+re};
    b.click(); await sleep(1200);
  }
  PCOSWin.isWindow=()=>true; PCOSWin.viewOf=()=>view;
  window.__opener=document.createElement('button'); document.body.appendChild(__opener);
  window.__panel=()=>document.querySelector('.osw-ai-panel');
  window.__open=async()=>{ if(__panel()) PCOS.pageWindowAI(__opener,{}); await sleep(150); PCOS.pageWindowAI(__opener,{}); await sleep(400); return !!__panel(); };
  window.__root=root;
  window.__snap=()=>({ modal:[...document.querySelectorAll('.modal-bg')].filter(m=>m.getClientRects().length && !m.closest('.osw-ai-panel')).map(m=>(m.innerText||'').replace(/\s+/g,' ').slice(0,160)).join(' | '),
    composer:[...document.querySelectorAll('.modal textarea')].map(t=>t.value).join(' | ').slice(0,200),
    note:(window.PCNotes&&PCNotes.current&&PCNotes.current())?JSON.stringify(PCNotes.current()).slice(0,300):'',
    found:document.querySelectorAll('.ai-found').length, outlined:document.querySelectorAll('.ai-target').length,
    toast:[...document.querySelectorAll('#toast-root > *, .toast')].map(t=>t.innerText).join(' | ').slice(0,200),
    fields:[...root().querySelectorAll('input:not([type=checkbox]):not([type=file]),textarea')].filter(i=>i.value&&!i.closest('.osw-ai-panel')).map(i=>(i.getAttribute('aria-label')||i.placeholder||i.name||'?')+'='+i.value.slice(0,50)).join('; ').slice(0,300),
    view:(__PC.VIEW||'')+' '+(document.getElementById('feed')||{innerText:''}).innerText.replace(/\s+/g,' ').slice(0,80) });
  if(!await __open()) return {error:'no panel'};
  return {labels:[...__panel().querySelectorAll('[data-ai-action]')].map(b=>({label:b.querySelector('b').textContent,starter:b.hasAttribute('data-starter')}))};
})"""
PRESS = r"""(async label=>{ if(!await __open()) return 'no panel';
  const b=[...__panel().querySelectorAll('[data-ai-action]')].find(x=>x.querySelector('b').textContent===label);
  if(!b) return 'not offered any more (an earlier button changed the window): '+[...__panel().querySelectorAll('[data-ai-action] b')].map(x=>x.textContent).join(' | ');
  b.click(); return ''; })"""
STATE = r"""(()=>{ const p=__panel(), a=p&&p.querySelector('.osw-ai-answer');
  return { q:__aiq.splice(0), open:!!p, busy:!!a&&a.classList.contains('loading'), shown:!!a&&!a.hidden,
           form:!!(a&&a.querySelector('form.osw-ai-find')), pick:!!(a&&a.querySelector('.osw-ai-pick [data-reply-to]')),
           text:a?(a.innerText||'').replace(/\s+/g,' ').trim().slice(0,700):'', err:!!a&&a.classList.contains('error'),
           buttons:a?[...a.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(Boolean).slice(0,14):[] }; })()"""
ACT = r"""(()=>{ const a=__panel()&&__panel().querySelector('.osw-ai-answer'); if(!a) return null;
  const act=a.querySelector('[data-apply],[data-use],[data-ai-all],[data-go],[data-act],[data-task-cal],[data-web-read],[data-ai-tasks-note],[data-ai-note],[data-reply-to],[data-open-note],[data-post]');
  if(!act) return null; window.__before=__snap(); const t=act.textContent.trim(); act.click(); return t; })()"""
DIFF = r"""(()=>{ const after=__snap(), out={}; for(const k of Object.keys(after)) if(JSON.stringify(after[k])!==JSON.stringify(__before[k])) out[k]=after[k];
  document.querySelectorAll('.modal-bg').forEach(m=>{ if(!m.closest('.osw-ai-panel')) m.remove(); }); document.body.classList.remove('modal-open'); return out; })()"""


async def answer(b, q):
    for item in q:
        try:
            body, status = {"ok": True, **(await dispatch(item["body"]))}, 200
        except svc.AssistError as e:
            body, status = {"ok": False, "error": e.detail}, e.status
        except Exception as e:                                   # noqa: BLE001
            body, status = {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500
        CALLS.append({"action": item["body"].get("action"), "recipe": item["body"].get("recipe"), "ok": body.get("ok")})
        await b.js("__aians[%d]=%s;true" % (item["id"], json.dumps({"status": status, "body": body})))


async def settle(b, row, view, queries, limit=240):
    """Run the page until its answer stops changing: answer parked requests, type into an inline box, pick
    the first post. Returns the last state."""
    st = {}
    for _ in range(limit):
        st = await b.js(STATE)
        if st["q"]:
            await answer(b, st["q"])
            continue
        if st["form"] and "query" not in row:
            lab = row["label"].lower()
            q = (queries["calc"] if "work it out" in lab else queries["contact"] if "contact" in lab else
                 queries["notes-find"] if "note" in view and "find" in lab else
                 queries["write"] if "note" in view else queries["find"])
            row["query"] = q
            await b.js("(()=>{const f=__panel().querySelector('form.osw-ai-find');f.querySelector('input').value=%s;f.requestSubmit();})()" % json.dumps(q))
            await asyncio.sleep(.3)
            continue
        if st["pick"] and "picked" not in row:
            row["picked"] = await b.js("(()=>{const p=__panel().querySelector('.osw-ai-pick [data-reply-to]');const t=p.innerText.slice(0,60);p.click();return t;})()")
            await asyncio.sleep(.3)
            continue
        if st["shown"] and not st["busy"]:
            await asyncio.sleep(.4)
            again = await b.js(STATE)
            if not again["q"] and not again["busy"]:
                return again
            continue
        await asyncio.sleep(.25)
    return st


async def press_all(b, view, queries):
    labels = (await b.js("__labels"))
    rows = []
    for k, lab in enumerate(labels):
        row = {"label": lab["label"]}
        rows.append(row)
        if lab["starter"]:
            row["starter"] = True
            continue
        why = await b.js(PRESS + "(%s)" % json.dumps(lab["label"]))
        if why:
            row["error"] = why
            continue
        await asyncio.sleep(.3)
        st = await settle(b, row, view, queries)
        if not st.get("shown"):
            row["error"] = "no answer"
            continue
        row.update(answer=st["text"], err=st["err"], buttons=st["buttons"])
        pressed = await b.js(ACT)
        if pressed:
            row["pressed"] = pressed
            st2 = await settle(b, row, view, queries, 120)
            if st2.get("text") and st2["text"] != st["text"]:
                row["after"] = st2["text"][:400]
                use = await b.js("(()=>{const u=__panel()&&__panel().querySelector('[data-use],[data-apply]');if(!u)return '';const t=u.textContent.trim();u.click();return t;})()")
                if use:
                    row["pressed"] += " → " + use
                    await settle(b, row, view, queries, 60)
            await asyncio.sleep(2.5)
            row["changed"] = await b.js(DIFF)
    return rows


def verdict(r):
    if r.get("starter"):
        return "STARTER"
    if r.get("error") or r.get("err"):
        return "ERROR"
    if not r.get("answer"):
        return "EMPTY"
    if r.get("changed"):
        return "USEFUL"
    return "ANSWER-ONLY" if not r.get("pressed") else "NO-EFFECT"


async def run(name, view, clicks, setup, port):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1200, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        # The feed seed fills the relay BEFORE login (the timeline reads it on entry); the fixture setups
        # borrowed from capture_window_ai_fixtures stub the app's API and need a signed-in client.
        before = setup == SEED_FEED
        if setup and before:
            await b.js(setup)
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        if setup and not before:
            await b.js(setup)
        prep = await b.js(PREP + "(%s,%s)" % (json.dumps(view), json.dumps(clicks)))
        if prep.get("error"):
            got["r"] = prep
            return
        await b.js("window.__labels=%s;true" % json.dumps(prep["labels"]))
        got["r"] = {"labels": prep["labels"], "rows": await press_all(b, view, QUERIES)}

    await desktop.with_browser("online", "", check, extra_init=RELAY)
    return got.get("r")


def main():
    want = [w for w in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if w]
    gen = desktop.bundle.__wrapped__(); next(gen)
    tally = {}
    try:
        for name, view, clicks, setup in SCENARIOS:
            if want and not any(w in name for w in want):
                continue
            t0 = time.time()
            try:
                r = asyncio.run(run(name, view, clicks, setup, 0))
            except Exception as e:                               # noqa: BLE001
                r = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
            if not r or r.get("error"):
                print(f"\n== {name} ({view}): COULD NOT RUN — {json.dumps(r)[:400]}", flush=True)
                tally["COULD-NOT-RUN"] = tally.get("COULD-NOT-RUN", 0) + 1
                continue
            print(f"\n== {name} ({view}) {time.time() - t0:.0f}s — buttons: " + " | ".join(l["label"] for l in r["labels"]), flush=True)
            for row in r["rows"]:
                v = verdict(row)
                tally[v] = tally.get(v, 0) + 1
                line = f"  [{v:11}] {row['label']}"
                if row.get("query"):
                    line += f"  (typed {row['query']!r})"
                if row.get("picked"):
                    line += f"  (picked {row['picked']!r})"
                print(line)
                for k in ("error", "answer", "after"):
                    if row.get(k):
                        print(f"      {k}: {row[k][:400]}")
                if row.get("buttons"):
                    print(f"      buttons: {row['buttons']}")
                if row.get("pressed"):
                    print(f"      pressed {row['pressed']!r} -> changed: {json.dumps(row.get('changed'), ensure_ascii=False)[:400]}")
            sys.stdout.flush()
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
