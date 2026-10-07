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

def api(*routes):
    """Setup JS: answer these instance API calls with canned JSON -- (url substring, body), first match
    wins -- so a screen that needs its server (Contacts with an addressbook, a Calendar that is on, search
    results) renders offline exactly as it does for somebody whose server answered."""
    return ("(()=>{const R=%s, real=window.fetch; window.fetch=(u,o)=>{const s=String(u);"
            "for(const [k,v] of R){ if(s.includes(k)) return Promise.resolve(new Response(JSON.stringify(v),"
            "{status:200,headers:{'Content-Type':'application/json'}})); } return real(u,o); };})();" % json.dumps(routes))


_VCARD = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nFN:{fn}\r\nN:{last};{first};;;\r\nTEL;TYPE=cell:{tel}\r\nEMAIL:{mail}\r\nEND:VCARD\r\n"
CONTACTS = api(["/api/contacts/books", {"books": [{"id": "personal", "displayname": "Personal", "kind": "VADDRESSBOOK"}]}],
               ["/api/contacts/cards", {"cards": [
                   {"uid": "c1", "ics": _VCARD.format(uid="c1", fn="Alice Jones", last="Jones", first="Alice", tel="555-0101", mail="alice@x.test")},
                   {"uid": "c2", "ics": _VCARD.format(uid="c2", fn="Carol White", last="White", first="Carol", tel="555-0199", mail="carol@x.test")}]}])
_VEVENT = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start}\r\nDTEND:{end}\r\nSUMMARY:{title}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
def _calendar():
    from datetime import date, timedelta
    d = date.today() + timedelta(days=2)
    s = d.strftime("%Y%m%d")
    return api(["/api/calendar/config", {"enabled": True, "url": "https://fixture.invalid/caldav/me/", "username": "me", "has_password": False}],
               ["/api/calendar/calendars", {"calendars": [{"id": "home", "displayname": "Home", "color": "#3fa9f5", "kind": "VCALENDAR"}]}],
               ["/api/calendar/items", {"items": [{"uid": "e1", "component": "VEVENT", "ics": _VEVENT.format(
                   uid="e1", start=s + "T140000", end=s + "T150000", title="Team standup")}]}])
CALENDAR = _calendar()

def _telegram():
    import time
    now = int(time.time())
    dana = [{"id": 11, "chat_id": 501, "out": False, "sender": "Dana", "date": now - 3600, "text": "Are we still on for the hike on Saturday?"},
            {"id": 12, "chat_id": 501, "out": True, "date": now - 3000, "text": "Yes! What time?"},
            {"id": 13, "chat_id": 501, "out": False, "sender": "Dana", "date": now - 600, "text": "How about 9am at the trailhead?"}]
    return api(["/api/tgc/status", {"configured": True, "state": "ready", "admin": False, "calls": False}],
               ["/api/tgc/ticket", {"ticket": "fixture"}],
               ["/api/tgc/dialogs", {"dialogs": [
                   {"id": 501, "title": "Dana", "kind": "user", "unread": 1, "last": {"id": 13, "text": dana[-1]["text"], "date": now - 600, "out": False}},
                   {"id": 502, "title": "Book club", "kind": "group", "unread": 0, "last": {"id": 7, "text": "Next book: Dune", "date": now - 86400, "out": False}}]}],
               ["/api/tgc/messages/501", {"messages": dana, "read_out": 12}],
               ["/api/tgc/messages/", {"messages": [], "read_out": 0}],
               ["/api/tgc/", {"ok": True}])
TELEGRAM = _telegram()

WEB_RESULTS = api(["/api/websearch/search", {"results": [
    {"title": "Wayfire configuration - Gentoo Wiki", "url": "https://wiki.gentoo.org/wiki/Wayfire", "content": "How to install and configure the Wayfire compositor on Gentoo.", "engine": "duckduckgo"},
    {"title": "wayfire.ini reference | Wayfire docs", "url": "https://wayfire.org/docs/wayfire-ini", "content": "Every option in wayfire.ini, with defaults.", "engine": "bing"},
    {"title": "My Wayfire setup on Gentoo (blog)", "url": "https://example.org/blog/wayfire-gentoo", "content": "A walk through my config files.", "engine": "brave"}],
    "answers": [], "suggestions": []}])

def _mail():
    m = lambda uid, subject, frm, ts, preview: {"uid": uid, "account": "me@home.test", "folder": "INBOX", "subject": subject,
                                                "from": frm, "ts": ts, "preview": preview, "read": True, "attachments": 0}
    return api(["/api/mail/accounts", {"accounts": [{"email": "me@home.test"}]}],
               ["/api/mail/folders", {"folders": ["INBOX", "Sent", "Drafts"], "sent": "Sent"}],
               ["/api/mail/messages", {"messages": [m("3", "Lunch on Friday?", "Dana <dana@x.test>", 300, "are you free"),
                                                    m("2", "Your receipt from the hardware store", "Store <shop@x.test>", 200, "thanks for shopping")],
                                       "next_until": 0}],
               ["/api/mail/", {"ok": True}])
MAIL = _mail()

MEDIA = api(["/api/media-center/lib1/folders", {"path": ".", "folders": []}],
            ["/api/media-center/lib1/items", {"revision": "1", "items": [
                {"id": "i1", "name": "Big Buck Bunny", "video": True, "folder": ".", "duration": 596},
                {"id": "i2", "name": "Sintel", "video": True, "folder": ".", "duration": 888}]}],
            ["/api/media-center", {"libraries": [{"id": "lib1", "name": "Movies", "kind": "video", "count": 2, "shared_with_me": False}],
                                   "can_create": False, "profiles": []}])

# A Concord room with two channels and a short conversation, the way the Concord full-app tests seed one.
CONCORD = r"""(()=>{
  const me=__PC.me().pubkey, other='b'.repeat(64), t=Math.floor(Date.now()/1000);
  window.__msgs=[
    {id:'m1'+'x'.repeat(62),pubkey:other,text:'Who is bringing the projector to the meetup tomorrow?',at:t-900,kind:9,tags:[]},
    {id:'m2'+'x'.repeat(62),pubkey:me,text:'I can, if someone grabs the HDMI cable.',at:t-600,kind:9,tags:[]},
    {id:'m3'+'x'.repeat(62),pubkey:other,text:'Deal. Doors open at 6pm.',at:t-300,kind:9,tags:[]}];
  const room={name:'Gentoo Users',communityId:'c'.repeat(64),naddr:'fixture-community',url:'https://fixture.invalid/invite/x#s',
    channels:[{id:'fixture-general',name:'general'},{id:'fixture-meetups',name:'meetups'}],cord:{bundle:{owner:me,relays:['wss://fixture.invalid']},hydrated:true}};
  localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:['6'.repeat(64)]},{id:'fixture-meetups',name:'meetups',streamPubkeys:['7'.repeat(64)]}]}),
    inspectChat:async()=>({messages:window.__msgs.slice(),reactions:[],reactionIds:[]}),
    createChatWrap:async(_b,_w,_c,text,_a,_s,tags,kind)=>({rumorId:'f'.repeat(64),wrap:{kind:1059,pubkey:'5'.repeat(64),id:'w'.repeat(64)},ms:Date.now(),tags})};
  __PC.relayPublishRoom=async()=>({ok:true,accepted:1,uncertain:false,msg:''});
})();"""

# name, view, steps to take first, extra setup JS. A step is a regex over a control's visible text (the
# first visible match is clicked -- inside an open dialog when there is one, else in the view), or
# "js:<expression>" run as is.
SCENARIOS = [
    ("calendar", "calendar", [], ""),
    ("calendar-on", "calendar", [], CALENDAR),
    ("calendar-new-event", "calendar", [r"^\+?\s*(new )?event$|add event"], CALENDAR),
    ("contacts", "contacts", [], ""),
    ("contacts-form", "contacts", [r"^\+?\s*(new )?contact$|add contact"], ""),
    ("contacts-book", "contacts", [], CONTACTS),
    ("contacts-new", "contacts", [r"^\+?\s*(new )?contact$|add contact"], CONTACTS),
    ("websearch", "websearch", [], ""),
    ("torrents", "torrents", [], ""),
    ("notes-editor", "notes", [r"^new note$"], ""),
    ("budget", "budget", [], ""),
    ("budget-form", "budget", [r"add bill|new bill|^\+\s*bill"], ""),
    ("calculator", "calculator", [], ""),
    # 2026-10-07: the windows the eval had never seen, and the in-between states of multi-round tasks.
    ("notes-start", "notes", [], ""),
    ("mail-compose", "mail", [r"^compose$"], MAIL),
    ("settings-relays", "settings", [r"^relays$"], ""),
    ("telegram", "tg", [], TELEGRAM),
    ("telegram-chat", "tg", [r"^D?\s*Dana"], TELEGRAM),
    ("websearch-results", "websearch", ["js:(()=>{const i=document.querySelector('#feed input');i.value='gentoo wayfire config';i.dispatchEvent(new Event('input',{bubbles:true}));})()", r"^search$"], WEB_RESULTS),
    ("concord", "concord", ["js:(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()"], CONCORD),
    ("files", "blossom", [], ""),
    ("files-new-folder", "blossom", [r"^new folder$"], ""),
    ("media-center", "media-center", [], MEDIA),
    ("translate", "translate", [], ""),
]
CAPTURE = r"""(async(view, clicks)=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  window.__cap=null; const real=window.fetch;
  window.fetch=(u,o)=>{ if(String(u).includes('/api/chat-assist')){ try{ const b=JSON.parse(o.body); if(b.action==='window_steps') window.__cap=b; }catch(_){}
      return Promise.resolve(new Response(JSON.stringify({ok:true,answer:'',tasks:[],steps:[]}),{status:200,headers:{'Content-Type':'application/json'}})); }
    return real(u,o); };
  __PC.switchView(view); await sleep(1500);
  const root=()=>{ const d=[...document.querySelectorAll('.modal-bg')].filter(m=>m.getClientRects().length).pop();
    return d?(d.querySelector('.modal')||d):document.getElementById('feed'); };
  for(const re of clicks){
    if(re.startsWith('js:')){ await (0,eval)(re.slice(3)); await sleep(1200); continue; }
    const rx=new RegExp(re,'i');
    let b=null;
    for(let i=0;i<40 && !b;i++){ b=[...root().querySelectorAll('button,a,[role=button],[role=tab]')].find(x=>x.getClientRects().length && rx.test((x.getAttribute('aria-label')||x.textContent||'').trim())); if(!b) await sleep(200); }
    if(!b) return {error:'no control matching '+re, labels:[...root().querySelectorAll('button')].map(x=>(x.getAttribute('aria-label')||x.textContent||'').trim()).filter(Boolean).slice(0,60)};
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show = "--print" in sys.argv                # look only: print what the window sends, write nothing
    want = args[0] if args else ""
    gen = desktop.bundle.__wrapped__(); next(gen)
    bad = 0
    try:
        for name, view, clicks, setup in SCENARIOS:
            if want and want not in name:
                continue
            r = asyncio.run(capture(name, view, clicks, setup))
            if not r or r.get("error") or not r.get("windows"):
                bad += 1
                print(f"{name:16} FAILED  {json.dumps(r)[:600]}", flush=True)
                continue
            r["instruction"] = "x"
            if show:
                print(f"== {name}: {r['windows'][0].get('text', '')[:600]}")
                for c in r.get("controls", []):
                    print(f"   [{c['ref']}] {c['role']} {c['label']!r}" + (f" = {c['value']!r}" if c.get("value") else "")
                          + (f" (in {c['near']!r})" if c.get("near") else ""))
                sys.stdout.flush()
                continue
            (OUT / f"{name}.json").write_text(json.dumps(r, indent=1, ensure_ascii=False) + "\n")
            print(f"{name:16} ok  {len(r.get('controls', []))} controls: "
                  + ", ".join(c['label'][:24] for c in r.get('controls', [])[:14]), flush=True)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
