"""A window's ✨ is an INTERACTIVE panel: answers, tasks and steps you run with buttons -- never AI Chat.

Reported: "ai actions from window sparkle, opens up ai chat but nothing happens", then "we need interactive
Agentic features with buttons, not loading up AI Chat", "Extract Tasks need to be functional and actually
useful" and "the text it does display is not really readable, need to be cyberpunk and nice".

Runs the real bundled client as a popped-out Social window (and the same page as a Terminal). The model's
reply is stubbed at /api/chat-assist; everything else is the shipped panel: the request is window_steps;
the answer is formatted (lists, not raw "- " text) and readable; tasks are a checklist that saves to Notes
and puts a dated task in the Calendar's own form; each step is a button and nothing happens until it is
pressed; ↻ Continue re-asks with what was done; and nothing ever hands off to the AI Chat screen.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


REPLY = {"ok": True, "answer": "Here is what this window needs:\n- First point\n- Second point with `code`",
         "tasks": [{"text": "Pay the Comcast invoice", "due": "2026-10-03", "who": "Dana"},
                   {"text": "Reply to Sam", "due": "", "who": ""}],
         "steps": [{"do": "insert", "label": "Insert a reply", "text": "Thanks Sam, on it."},
                   {"do": "note", "label": "Keep the summary", "text": "Two invoices due"},
                   {"do": "open", "label": "Open Calendar", "text": "calendar"}]}

STUB = r"""(()=>{
window.__req=[]; window.__reply={status:200, body:%s};
const realFetch=window.fetch;
window.fetch=(url,opts)=>{ if(String(url).includes('/api/chat-assist') && !String(opts.body).includes('window_event')){ __req.push(JSON.parse(opts.body));
    return new Promise(r=>setTimeout(()=>r(new Response(JSON.stringify(__reply.body),{status:__reply.status,headers:{'Content-Type':'application/json'}})),250)); }
  return realFetch(url,opts); };
window.__asked=[]; PCOSWin.desktop=()=>({ __PC:{ askWindowContext:(ctx,ins,opt)=>{ __asked.push({ctx,ins,opt}); } } });
window.__aiView=0; {const sv=__PC.switchView; __PC.switchView=v=>{ if(v==='ai') __aiView++; return sv(v); };}
window.__copied=[]; __PC.copyValue=v=>{ __copied.push(v); return Promise.resolve(true); };
window.__notes=[]; window.PCNotes=Object.assign(window.PCNotes||{}, {save:async n=>{ __notes.push(n); return {ok:true}; }});
window.__realDraft=window.PCCalendar&&window.PCCalendar.draft; window.__drafts=[]; window.PCCalendar=Object.assign(window.PCCalendar||{}, {draft:async e=>{ __drafts.push(e); }});
window.__confirm=false; __PC.uiConfirm=async()=>__confirm;
const f=document.getElementById('feed'); const t=document.createElement('textarea'); t.id='t-box'; t.style.cssText='display:block;width:200px;height:40px';
f.appendChild(t); t.focus();
})()
""" % __import__("json").dumps(REPLY)

NEVER_AI = "({asked:__asked.length, aiView:__aiView, view:__PC.VIEW})"


async def _open(b, width, height, view="global", label="Social"):
    await desktop.login(b)
    await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 600})
    await b.until("!!(window.PCOSWin && PCOSWin.isWindow())")
    await b.js("document.getElementById('pc-oswin-chrome') || PCOSWin.adopt({view:'%s', label:'%s'})" % (view, label))
    await b.until("!!document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]')")
    await b.js(STUB)
    await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click()")
    await b.until("!!document.querySelector('.osw-ai-panel')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(1100, 760), (420, 700)])
def test_the_sparkle_opens_the_panel_and_never_hands_off_to_ai_chat(width, height):
    async def check(b):
        await _open(b, width, height)
        fit = await b.js("""(()=>{const p=document.querySelector('.osw-ai-panel').getBoundingClientRect();
          return {fits:p.left>=0&&p.right<=innerWidth+1&&p.top>=38, actions:document.querySelectorAll('.osw-ai-panel [data-ai-action]').length,
                  title:(document.querySelector('.osw-ai-panel header b')||{}).textContent||'',
                  open:!!document.querySelector('[data-ai-open]')}})()""")
        assert fit["fits"] and fit["actions"] == 3 and "Social" in fit["title"], fit
        assert fit["open"] is False, "'Open in AI' is back"
        view0 = await b.js("__PC.VIEW")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-text')")
        req = await b.js("__req[0]")
        # The first button on a feed is a RECIPE now (a summary with a Save-to-Notes button built on the
        # server), not a canned prompt through step planning -- see test_window_ai_recipes*.
        assert req["action"] == "window_recipe" and req["recipe"] == "summary" and req["windows"][0]["kind"] == "PosterChan app", req
        assert await b.js(NEVER_AI) == {"asked": 0, "aiView": 0, "view": view0}

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(1100, 760), (420, 700)])
def test_the_answer_is_readable_tasks_are_a_working_checklist_and_steps_are_buttons(width, height):
    async def check(b):
        await _open(b, width, height)
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-text')")
        got = await b.js(r"""(()=>{const a=document.querySelector('.osw-ai-answer'), t=document.querySelector('.osw-ai-text');
          const p=t.querySelector('li'), cs=getComputedStyle(p), bg=getComputedStyle(a);
          const rgb=s=>(s.match(/\d+(\.\d+)?/g)||[]).slice(0,3).map(Number);
          const lum=c=>{const v=c.map(x=>{x/=255;return x<=.03928?x/12.92:Math.pow((x+.055)/1.055,2.4)});return .2126*v[0]+.7152*v[1]+.0722*v[2]};
          // The card's background is a gradient over a dark panel: measure against the darkest it gets.
          const fg=lum(rgb(cs.color)), back=0.0;
          const r=a.getBoundingClientRect();
          return {lis:[...t.querySelectorAll('li')].map(x=>x.textContent), raw:t.textContent.includes('- First'), code:!!t.querySelector('code'),
                  size:parseFloat(cs.fontSize), contrast:(fg+.05)/(back+.05), fits:r.left>=0&&r.right<=innerWidth+1,
                  tasks:[...a.querySelectorAll('.osw-ai-tasks li')].map(li=>li.querySelector('label span').textContent),
                  chips:[...a.querySelectorAll('.osw-ai-chips i')].map(i=>i.textContent),
                  steps:[...a.querySelectorAll('.osw-ai-step')].map(s=>s.querySelector('.osw-ai-step-h b').textContent)}})()""")
        assert got["lis"] == ["First point", "Second point with code"] and got["raw"] is False and got["code"], got
        assert got["size"] >= 14 and got["contrast"] >= 7 and got["fits"], got
        assert got["tasks"] == ["Pay the Comcast invoice", "Reply to Sam"] and got["chips"] == ["2026-10-03", "Dana"], got
        assert got["steps"] == ["Insert a reply", "Keep the summary", "Open Calendar"], got
        assert await b.js("document.getElementById('t-box').value") == "" and await b.js("__notes.length") == 0, \
            "nothing happens until a button is pressed"

        await b.js("document.querySelector('[data-ai-tasks-note]').click()")
        await b.until("__notes.length===1")
        note = await b.js("__notes[0]")
        assert note["body"] == "- [ ] Pay the Comcast invoice (due 2026-10-03) — Dana\n- [ ] Reply to Sam" and "Social" in note["title"], note

        # Insert asks before replacing what is typed; Yes puts the step's text in, never sent.
        await b.js("document.getElementById('t-box').value='my own words'")
        await b.js("document.querySelector('.osw-ai-step[data-do=\"insert\"] [data-go]').click()")
        await asyncio.sleep(.3)
        assert await b.js("document.getElementById('t-box').value") == "my own words"
        await b.js("__confirm=true; document.querySelector('.osw-ai-step[data-do=\"insert\"] [data-go]').click()")
        await b.until("document.getElementById('t-box').value==='Thanks Sam, on it.'")
        assert await b.js("document.querySelector('.osw-ai-step[data-do=\"insert\"]').classList.contains('done')")

        # ↻ Continue re-reads the window and carries what was done.
        await b.js("document.querySelector('[data-ai-continue]').click()")
        await b.until("__req.length===2")
        nxt = await b.js("__req[1]")
        assert nxt["instruction"].startswith("Continue") and nxt["history"][0]["did"] == ["saved the task list to Notes", "Insert a reply"], nxt
        await b.until("!!document.querySelector('[data-task-cal]')")

        # A dated task goes into the Calendar's own form, filled in -- no AI round trip, nothing saved.
        n = await b.js("__req.length")
        await b.js("document.querySelector('[data-task-cal]').click()")
        await b.until("__drafts.length===1")
        assert await b.js("__drafts[0]") == {"title": "Pay the Comcast invoice", "date": "2026-10-03", "start": "", "end": "",
                                              "allDay": True, "location": "", "notes": "Who: Dana"}
        assert await b.js("__req.length") == n
        assert await b.js(NEVER_AI + ".asked") == 0 and await b.js("__aiView") == 0

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_refusal_is_said_in_the_panel():
    async def check(b):
        await _open(b, 1100, 760)
        await b.js("__reply={status:403, body:{ok:false, error:'AI access is not enabled for this account'}}")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"1\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-answer.error')")
        assert "not enabled" in await b.js("document.querySelector('.osw-ai-answer').textContent")
        assert await b.js("__asked.length") == 0
    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_terminal_offers_run_and_type_buttons_and_other_windows_never_ask_for_commands():
    async def check(b):
        await _open(b, 1100, 760)
        await b.js("window.__ran=[];window.__typed=[];window.PCTerm=Object.assign(window.PCTerm||{}, {connected:()=>true,"
                   "typeIn:t=>{ __typed.push(t); return true; }, run:t=>{ __ran.push(t); return true; }});"
                   "__reply={status:200, body:{ok:true, answer:'The disk is full.', tasks:[], "
                   "steps:[{do:'command',label:'Show disk use',text:'df -h'}]}};")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await b.until("__req.length===1")
        # A Social button is a recipe now, which carries no `commands` at all -- either way, never true.
        assert await b.js("!!__req[0].commands") is False, "a Social window must not be offered commands"
        await b.js("document.querySelector('[data-ai-dismiss]').click()")
        await b.js("PCOSWin.viewOf=()=>'terminal'")
        await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-panel')")
        assert "Explain output" in await b.js("document.querySelector('.osw-ai-actions').textContent")
        assert await b.js("document.querySelector('[data-ai-cmds]').checked") is True
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-step [data-run]')")
        assert await b.js("__req[1].commands") is True
        assert await b.js("document.querySelector('.osw-ai-step pre code').textContent") == "df -h"
        assert await b.js("__ran.length") == 0, "nothing runs on its own"
        await b.js("document.querySelector('.osw-ai-step [data-run]').click()")
        await b.until("__ran.length===1")
        assert await b.js("__ran[0]") == "df -h" and await b.js("document.querySelector('.osw-ai-step').classList.contains('done')")
        assert await b.js(NEVER_AI + ".aiView") == 0
    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))


CAL_STUB = r"""(()=>{
const inner=window.fetch;
window.__calReq=[]; window.__calWrites=[];
window.fetch=(url,opts)=>{ const u=String(url);
  if(u.includes('/api/calendar/') && opts && opts.method && opts.method!=='GET') __calWrites.push(opts.method+' '+u);
  if(u.includes('/api/chat-assist') && opts && String(opts.body).includes('window_event')){
    __calReq.push(JSON.parse(opts.body));
    return Promise.resolve(new Response(JSON.stringify({ok:true,event:{title:'Dentist',date:'2026-10-02',start:'14:30',end:'15:00',allDay:false,location:'Main St',notes:''}}),{status:200,headers:{'Content-Type':'application/json'}}));
  }
  const j=o=>Promise.resolve(new Response(JSON.stringify(o),{status:200,headers:{'Content-Type':'application/json'}}));
  if(u.includes('/api/calendar/config')) return j({enabled:true});
  if(u.includes('/api/calendar/calendars')) return j({calendars:[{id:'personal',displayname:'Personal'}]});
  if(u.includes('/api/calendar/items')) return j({items:[]});
  return inner(url,opts); };
})()"""

@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(1100, 760), (420, 700)])
def test_a_calendar_step_opens_the_calendars_own_form_filled_in_and_saves_nothing(width, height):
    async def check(b):
        await _open(b, width, height)
        await b.js("window.PCCalendar.draft=window.__realDraft")      # the REAL Calendar form this time
        await b.js(CAL_STUB)
        await b.js("__reply={status:200, body:{ok:true, answer:'You have a dentist appointment.', tasks:[], "
                   "steps:[{do:'calendar',label:'Add the appointment',text:'Dentist on Main St, Oct 2 at 2:30pm'}]}};")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-step[data-do=\"calendar\"] [data-go]')")
        await b.js("document.querySelector('.osw-ai-step[data-do=\"calendar\"] [data-go]').click()")
        await b.until("!!document.getElementById('cev-title')")
        form = await b.js("""(()=>{const v=id=>document.getElementById(id).value;
          const r=document.getElementById('cev-title').closest('.modal, .modal-box, [role=dialog]') || document.getElementById('cev-title');
          const b=r.getBoundingClientRect();
          return {title:v('cev-title'),date:v('cev-date'),start:v('cev-start'),end:v('cev-end'),loc:v('cev-loc'),
                  cal:v('cev-cal'), allday:document.getElementById('cev-allday').checked,
                  fits:b.left>=0&&b.right<=innerWidth+1, del:!!document.getElementById('cev-del')}})()""")
        assert form == {"title": "Dentist", "date": "2026-10-02", "start": "14:30", "end": "15:00", "loc": "Main St",
                        "cal": "personal", "allday": False, "fits": True, "del": False}, form
        req = await b.js("__calReq[0]")
        assert req["today"] and req["answer"] == "Dentist on Main St, Oct 2 at 2:30pm" and req["windows"][0]["title"], req
        assert await b.js("!document.querySelector('.osw-ai-panel')"), "the panel would cover the form"
        await asyncio.sleep(.4)
        assert await b.js("__calWrites") == [], "nothing is saved until the person presses Save"
        await b.js("document.getElementById('cev-title').value='Dentist cleaning'")
        await b.js("[...document.querySelectorAll('button')].find(x=>/^\\s*Save\\s*$/.test(x.textContent)).click()")
        await b.until("__calWrites.length>=1")
        assert await b.js("__calWrites.length") == 1 and "/api/calendar/items" in await b.js("__calWrites[0]")
    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))
