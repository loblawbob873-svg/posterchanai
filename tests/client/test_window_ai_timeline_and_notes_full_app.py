"""✨ on the timeline and in Notes DOES something: it names posts and notes, and its buttons change them.

Reported: "agentic window features are still kinda useless for the social timeline" and "useless in notes".
On a timeline the panel had three generic buttons over the screen's innerText, so nothing it said pointed at
a post and nothing could be answered. In Notes an open note is shown RENDERED with its textarea hidden, and
the panel looked for a visible text box -- so for any note that already had text it found no note at all,
never offered Tidy, and summarised the screen instead.

The AI's answers here are canned (what the model's JSON looks like after chat_assist_service parsed it); the
model itself is scored by scripts/audit_window_ai_buttons.py. What these assert is what the PERSON gets:
a reply drafted for Bob's post opens Bob's reply box with that text, unsent; tidying an open note changes the
note in the notebook, and Undo puts it back.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


# The panel's AI calls answered from a table, and every request kept for the assertions.
STUB_AI = r"""(answers=>{
  window.__aiSent=[];
  const reply=b=>{ __aiSent.push(b); const k=b.action+':'+(b.recipe||''); const a=answers[k];
    return new Response(JSON.stringify(a?{ok:true,...a}:{ok:false,error:'no canned answer for '+k}),{status:a?200:400,headers:{'Content-Type':'application/json'}}); };
  const af=__PC.authFetch; __PC.authFetch=(u,o)=>String(u).includes('/api/chat-assist')?Promise.resolve(reply(JSON.parse(o.body))):af(u,o);
  __PC.ensureAiSession=async()=>true;
  PCOSWin.isWindow=()=>true;
})"""
OPEN_PANEL = ("(()=>{PCOSWin.viewOf=()=>%s;const b=document.createElement('button');document.body.appendChild(b);"
              "PCOS.pageWindowAI(b,{});return [...document.querySelectorAll('.osw-ai-panel [data-ai-action] b')].map(x=>x.textContent)})()")
CLICK = "(()=>{const b=[...document.querySelectorAll('.osw-ai-panel %s')].find(x=>x.textContent.includes(%s));if(b)b.click();return !!b})()"

POSTS = [(2, "Alice", "Anyone know a good Wayland compositor for an old laptop?", False),
         (3, "Bob", "did you ever get the Arc A770 running llama.cpp with SYCL?", True),
         (4, "Carol", "Bitcoin fees are down to 2 sat/vB again.", False)]
SEED = r"""(()=>{const me=NostrTools.getPublicKey(new Uint8Array(32).fill(1)), now=Math.floor(Date.now()/1000), r=_rel();
  for(const [sk,name,text,toMe] of %s){ const k=new Uint8Array(32).fill(sk);
    r.push(NostrTools.finalizeEvent({kind:0,created_at:now-9999,tags:[],content:JSON.stringify({name})},k));
    r.push(NostrTools.finalizeEvent({kind:1,created_at:now-600+sk,tags:toMe?[['p',me]]:[],content:text},k)); }
  localStorage.setItem('__relayEvents',JSON.stringify(r));})()""" % json.dumps(POSTS)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_timeline_panel_drafts_a_reply_into_that_posts_reply_box():
    got = {}
    canned = {"window_feed:needs": {"feed": {"kind": "needs", "items": [{"n": 2, "why": "asks you a question"}]}},
              "window_feed:reply": {"feed": {"kind": "reply", "replies": ["Yes — SYCL build of llama.cpp, notes coming.",
                                                                          "Not yet, did you?", "Only after three kernels."]}}}

    async def check(b):
        await b.js(SEED)
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('global');true")
        await b.until("document.querySelectorAll('#feed article.note[data-id]').length>=3")
        # The names arrive with the profiles, a moment after the cards; the panel reads them as shown.
        for _ in range(100):
            if await b.js("[...document.querySelectorAll('#feed article.note .hd .name')].some(n=>n.textContent.includes('Bob'))"):
                break
            await asyncio.sleep(.1)
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        got["buttons"] = await b.js(OPEN_PANEL % json.dumps("global"))
        await b.js(CLICK % ("[data-ai-action]", json.dumps("What needs me")))
        await b.until("!!document.querySelector('.osw-ai-panel [data-reply-to]')")
        got["sent"] = await b.js("__aiSent[0]")
        await b.js(CLICK % ("[data-reply-to]", json.dumps("Draft reply")))
        await b.until("!!document.querySelector('.osw-ai-panel [data-use]')")
        await b.js(CLICK % ("[data-use]", json.dumps("Use this")))
        # 10 s: the composer is a lazily-loaded module on a loaded gate.
        for _ in range(100):
            if await b.js("[...document.querySelectorAll('.modal textarea')].some(t=>t.value.includes('SYCL build'))"):
                break
            await asyncio.sleep(.1)
        got["composer"] = await b.js("[...document.querySelectorAll('.modal textarea')].map(t=>t.value).join('|')")
        got["modal"] = await b.js("[...document.querySelectorAll('.modal')].map(m=>m.innerText).join('|')")
        got["published"] = await b.js("(window.__published||[]).filter(e=>e.kind===1).length")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    assert "What needs me" in got["buttons"] and "Draft a reply…" in got["buttons"], got["buttons"]
    sent = got["sent"]
    assert sent["action"] == "window_feed" and len(sent["posts"]) >= 3, sent
    bob = next(p for p in sent["posts"] if "SYCL" in p["text"])
    assert bob["who"] == "Bob" and bob["to_me"] is True, bob            # it knows the post is addressed to me
    assert "SYCL build" in got["composer"], ("the drafted reply did not reach a reply box", got)
    assert "Bob" in got["modal"], ("the reply box is not a reply to Bob's post", got["modal"][:300])
    assert got["published"] == 0, "the drafted reply was SENT -- it must only be put in the box"


NOTE_BODY = "lisbon in spring? or porto. check flights. ask dana if she wants to come"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_notes_panel_tidies_the_open_note_and_undo_puts_it_back():
    got = {}
    canned = {"window_note:tidy": {"note": {"kind": "tidy", "body": "- Lisbon in spring, or Porto\n- Check flights\n- Ask Dana"}}}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('notes');true")
        await b.until("!!(window.PCNotes && PCNotes.save) && !!document.querySelector('.nt-wrap')")
        await b.js("(async()=>{const r=await PCNotes.save({title:'Trip ideas',body:%s});await PCNotes.select(r.id);window.__nid=r.id;})()" % json.dumps(NOTE_BODY))
        await b.until("!!document.querySelector('.nt-editor') && PCNotes.current() && PCNotes.current().id===__nid")
        await asyncio.sleep(.5)
        # The note is open RENDERED: its textarea is not on screen (this is what the panel used to miss).
        got["textarea_hidden"] = await b.js("![...document.querySelectorAll('.nt-editor textarea')].some(t=>t.getClientRects().length)")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        got["buttons"] = await b.js(OPEN_PANEL % json.dumps("notes"))
        await b.js(CLICK % ("[data-ai-action]", json.dumps("Tidy this note")))
        await b.until("!!document.querySelector('.osw-ai-panel [data-apply]')")
        got["sent"] = await b.js("__aiSent[0]")
        await b.js(CLICK % ("[data-apply]", json.dumps("Replace the note")))
        for _ in range(100):
            if await b.js("PCNotes.current().body.includes('Check flights')"):
                break
            await asyncio.sleep(.1)
        got["after"] = await b.js("PCNotes.current()")
        for _ in range(100):                     # the read view renders its markdown a moment later
            if await b.js("(document.querySelector('.nt-editor')||{innerText:''}).innerText.includes('Check flights')"):
                break
            await asyncio.sleep(.1)
        got["shown"] = await b.js("(document.querySelector('.nt-editor')||{innerText:''}).innerText")
        got["dbg"] = await b.js("({isView:__PC.isView&&__PC.isView('notes'), sel:PCNotes.current().id===__nid, ta:(document.querySelector('.nt-editor textarea')||{}).value})")
        await b.until("!!document.querySelector('.osw-ai-panel [data-undo-n]:not([hidden])')")
        await b.js(CLICK % ("[data-undo-n]", json.dumps("Undo")))
        for _ in range(100):
            if await b.js("PCNotes.current().body===%s" % json.dumps(NOTE_BODY)):
                break
            await asyncio.sleep(.1)
        got["undone"] = await b.js("PCNotes.current()")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    assert got["textarea_hidden"], "fixture: the note was expected to open rendered"
    assert "Tidy this note" in got["buttons"], ("an open note with text offers no Tidy", got["buttons"])
    assert got["sent"]["action"] == "window_note" and got["sent"]["text"] == NOTE_BODY, got["sent"]
    assert got["after"]["body"].startswith("- Lisbon") and got["after"]["title"] == "Trip ideas", got["after"]
    assert "Check flights" in got["shown"], ("the open note did not repaint with the tidied text", got["dbg"])
    assert got["undone"]["body"] == NOTE_BODY, ("Undo did not put the note back", got["undone"])


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_calculator_works_it_out_with_its_own_arithmetic():
    """Pressing keypad buttons, the free-form agent got "× 7" for a tip question (measured 0/3). The model now only
    TRANSLATES the words into an expression; the calculator's evaluator does the maths and the answer lands in it."""
    got = {}
    canned = {"window_calc:": {"calc": {"expression": "84.5*15/100/3", "what": "tip per person"}}}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('calculator');true")
        await b.until("!!window.PCCalc && !!document.querySelector('.calc-screen')")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        got["buttons"] = await b.js(OPEN_PANEL % json.dumps("calculator"))
        await b.js(CLICK % ("[data-ai-action]", json.dumps("Work it out")))
        await b.until("!!document.querySelector('.osw-ai-panel form.osw-ai-find')")
        await b.js("(()=>{const f=document.querySelector('.osw-ai-panel form.osw-ai-find');f.querySelector('input').value='15% tip on 84.50 split 3 ways';f.requestSubmit();})()")
        await b.until("!!document.querySelector('.osw-ai-panel [data-ai-calc-put]')")
        got["shown"] = await b.js("document.querySelector('.osw-ai-panel .osw-ai-calc').innerText")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-calc-put]').click()")
        await asyncio.sleep(.3)
        got["state"] = await b.js("PCCalc.state()")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    assert "Work it out…" in got["buttons"] and "Explain this result" not in got["buttons"], got["buttons"]
    assert "4.225" in got["shown"], got["shown"]                       # 84.5 × 15% ÷ 3, by the calculator
    assert got["state"]["expr"] == "4.225", ("the answer did not land in the calculator", got["state"])


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_add_a_contact_opens_the_new_contact_form_filled_in():
    """The free-form agent pressed "+ Contact" and then could not see the form it had opened (measured 0/3). One
    sentence now becomes the app's own New contact form, filled in, for the person to check and Save."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import capture_window_ai_fixtures as cap
    got = {}
    canned = {"window_contact:": {"contact": {"given": "Bob", "family": "Smith", "phone": "555-123-4567",
                                              "email": "bob@acme.test", "org": "Acme", "note": ""}}}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(cap.CONTACTS)
        await b.js("__PC.switchView('contacts');true")
        await b.until("!!window.PCContacts && document.body.innerText.includes('Alice Jones')")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        got["buttons"] = await b.js(OPEN_PANEL % json.dumps("contacts"))
        await b.js(CLICK % ("[data-ai-action]", json.dumps("Add a contact")))
        await b.until("!!document.querySelector('.osw-ai-panel form.osw-ai-find')")
        await b.js("(()=>{const f=document.querySelector('.osw-ai-panel form.osw-ai-find');f.querySelector('input').value='Bob Smith from Acme 555-123-4567 bob@acme.test';f.requestSubmit();})()")
        await b.until("!!document.getElementById('cc-given')")
        got["form"] = await b.js("({given:cc_v('#cc-given'),family:cc_v('#cc-family'),org:cc_v('#cc-org'),"
                                 "vals:[...document.querySelectorAll('.ct-mv')].map(i=>i.value)})".replace(
                                     "cc_v(", "((s)=>document.querySelector(s).value)("))
        got["saved"] = await b.js("(window.__published||[]).length")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    assert "Add a contact…" in got["buttons"], got["buttons"]
    f = got["form"]
    assert (f["given"], f["family"], f["org"]) == ("Bob", "Smith", "Acme"), f
    assert "555-123-4567" in f["vals"] and "bob@acme.test" in f["vals"], f


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_add_an_event_and_add_a_bill_open_the_apps_own_forms_filled_in():
    """"Add an event…" was a starter for the free-form agent, which had to open the form and then plan fields it
    had not seen; Budget had no way to add a bill from a sentence at all. Both now read ONE sentence and open the
    app's own form filled in -- the New event form, Budget's editable bill review -- with nothing saved."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import capture_window_ai_fixtures as cap
    got = {}
    canned = {"window_event:": {"event": {"title": "Dentist", "date": "2026-10-13", "start": "15:00", "end": "",
                                          "allDay": False, "location": "", "notes": ""}}}
    BILL = ("(()=>{const a=__PC.authFetch; __PC.authFetch=(u,o)=>String(u).includes('/api/budget/scan')"
            "?Promise.resolve(new Response(JSON.stringify({type:'bill',vendor:'Electric Co',amount:84.2,due_day:15}),{status:200,headers:{'Content-Type':'application/json'}})):a(u,o);})();true")

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(cap.CALENDAR)
        await b.js("__PC.switchView('calendar');true")
        await b.until("!!window.PCCalendar")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        got["cal_buttons"] = await b.js(OPEN_PANEL % json.dumps("calendar"))
        await b.js(CLICK % ("[data-ai-action]", json.dumps("Add an event")))
        await b.until("!!document.querySelector('.osw-ai-panel form.osw-ai-find')")
        await b.js("(()=>{const f=document.querySelector('.osw-ai-panel form.osw-ai-find');f.querySelector('input').value='dentist next tuesday 3pm';f.requestSubmit();})()")
        for _ in range(100):
            if await b.js("[...document.querySelectorAll('.modal input')].some(i=>i.value==='Dentist')"):
                break
            await asyncio.sleep(.1)
        got["event_form"] = await b.js("[...document.querySelectorAll('.modal input')].map(i=>i.value).join('|')")
        await b.js("document.querySelectorAll('.modal-bg').forEach(m=>m.remove()); document.body.classList.remove('modal-open'); true")
        # Budget.
        await b.js("__PC.switchView('budget');true")
        await b.until("!!window.PCBudget")
        await b.js(BILL)
        got["bud_buttons"] = await b.js(OPEN_PANEL % json.dumps("budget"))
        await b.js(CLICK % ("[data-ai-action]", json.dumps("Add a bill")))
        await b.until("!!document.querySelector('.osw-ai-panel form.osw-ai-find')")
        await b.js("(()=>{const f=document.querySelector('.osw-ai-panel form.osw-ai-find');f.querySelector('input').value='electric bill $84.20 due the 15th';f.requestSubmit();})()")
        for _ in range(100):
            if await b.js("!!document.getElementById('bg-an')"):
                break
            await asyncio.sleep(.1)
        got["bill_form"] = await b.js("({name:(document.getElementById('bg-an')||{}).value, amount:(document.getElementById('bg-aa')||{}).value})")
        got["published"] = await b.js("(window.__published||[]).length")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    assert "Add an event…" in got["cal_buttons"], got["cal_buttons"]
    assert "Dentist" in got["event_form"] and "2026-10-13" in got["event_form"], ("the New event form was not filled in", got["event_form"])
    assert "Add a bill…" in got["bud_buttons"], got["bud_buttons"]
    assert got["bill_form"] == {"name": "Electric Co", "amount": "84.20"}, got["bill_form"]


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_which_results_answer_it_names_results_that_open_in_the_reader():
    """The answer used to be prose naming results it liked, linked to none of them. The result cards now go out
    numbered with the search, and each pick opens THAT result in the reader."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import capture_window_ai_fixtures as cap
    got = {}
    canned = {"window_feed:needs": {"feed": {"kind": "needs", "items": [{"n": 2, "why": "every option with defaults"}]}}}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(cap.WEB_RESULTS)
        await b.js("__PC.switchView('websearch');true")
        await b.until("!!document.getElementById('ws-q')")
        await b.js("(()=>{const i=document.getElementById('ws-q');i.value='gentoo wayfire config';i.dispatchEvent(new Event('input',{bubbles:true}));i.form.requestSubmit();})()")
        await b.until("document.querySelectorAll('.ws-card[data-i]').length>=3")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        got["buttons"] = await b.js(OPEN_PANEL % json.dumps("websearch"))
        await b.js(CLICK % ("[data-ai-action]", json.dumps("Which results answer it")))
        await b.until("!!document.querySelector('.osw-ai-panel [data-web-read]')")
        got["sent"] = await b.js("__aiSent[0]")
        got["answer"] = await b.js("document.querySelector('.osw-ai-panel .osw-ai-answer').innerText")
        await b.js("document.querySelector('.osw-ai-panel [data-web-read]').click(); true")
        for _ in range(100):
            if await b.js("!!document.querySelector('.ws-reader')"):
                break
            await asyncio.sleep(.1)
        got["reader"] = await b.js("(document.querySelector('.ws-reader .ws-rtitle')||document.querySelector('.ws-reader')||{innerText:''}).innerText")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    assert got["sent"]["subject"] == "results" and got["sent"]["instruction"] == "gentoo wayfire config", got["sent"]
    assert len(got["sent"]["posts"]) >= 3 and "Wayfire" in got["sent"]["posts"][0]["text"], got["sent"]["posts"]
    assert "wayfire.ini reference" in got["answer"] and "every option with defaults" in got["answer"], got["answer"]
    assert "wayfire.ini" in got["reader"], ("Read here did not open the picked result", got["reader"])


ASK = ("(()=>{const ta=document.querySelector('.osw-ai-panel textarea');ta.value=%s;"
       "document.querySelector('.osw-ai-panel [data-ai-ask]').click();})(); true")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_typed_request_goes_to_the_tool_that_does_it():
    """Typed into the ✨ box, "add Bob Smith …" in Contacts or a sum in the Calculator went to the free-form
    agent, which the eval scored 0/3 on both. A request that plainly IS one of those jobs now goes to the same
    direct tool its button uses; anything else still goes to the agent."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import capture_window_ai_fixtures as cap
    got = {}
    canned = {"window_contact:": {"contact": {"given": "Bob", "family": "Smith", "phone": "555-123-4567", "email": "", "org": "", "note": ""}},
              "window_calc:": {"calc": {"expression": "84.5*15/100", "what": "15% of 84.50"}},
              "window_steps:": {"answer": "ok", "tasks": [], "steps": []}}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(cap.CONTACTS)
        await b.js("__PC.switchView('contacts');true")
        await b.until("!!window.PCContacts && document.body.innerText.includes('Alice Jones')")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        await b.js(OPEN_PANEL % json.dumps("contacts"))
        await b.js(ASK % json.dumps("add Bob Smith 555-123-4567"))
        for _ in range(100):
            if await b.js("!!document.getElementById('cc-given')"):
                break
            await asyncio.sleep(.1)
        got["contact"] = await b.js("({given:(document.getElementById('cc-given')||{}).value, sent:__aiSent.map(x=>x.action)})")
        await b.js("document.querySelectorAll('.modal-bg').forEach(m=>m.remove()); document.body.classList.remove('modal-open'); true")
        await b.js("__PC.switchView('calculator');true")
        await b.until("!!window.PCCalc && !!document.querySelector('.calc-screen')")
        await b.js("__aiSent.length=0; " + OPEN_PANEL % json.dumps("calculator"))
        await b.js(ASK % json.dumps("what is 15% of 84.50"))
        await b.until("!!document.querySelector('.osw-ai-panel .osw-ai-calc')")
        got["calc"] = await b.js("({shown:document.querySelector('.osw-ai-panel .osw-ai-calc').innerText, sent:__aiSent.map(x=>x.action)})")
        # Not a job with a tool: still the agent.
        # ✨ is a TOGGLE: the calculation's answer leaves the panel open, so reopening without closing it shut it.
        await b.js("(document.querySelector('.osw-ai-panel [data-ai-dismiss]')||{click(){}}).click();true")
        await b.js("__aiSent.length=0; " + OPEN_PANEL % json.dumps("calculator"))
        await b.js(ASK % json.dumps("explain how percentages work"))
        for _ in range(100):
            if await b.js("__aiSent.length>0"):
                break
            await asyncio.sleep(.1)
        got["other"] = await b.js("__aiSent.map(x=>x.action)")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    assert got["contact"]["given"] == "Bob" and got["contact"]["sent"] == ["window_contact"], got["contact"]
    assert "12.675" in got["calc"]["shown"] and got["calc"]["sent"] == ["window_calc"], got["calc"]
    assert got["other"] == ["window_steps"], got["other"]
