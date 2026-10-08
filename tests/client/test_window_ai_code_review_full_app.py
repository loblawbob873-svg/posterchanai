"""Window ✨: the code review's findings, each driven in the shipped client (2026-10-07).

  * a typed request answered by a direct tool (calculator, event, bill, contact, find) was rendered into an
    answer box that stayed HIDDEN -- a spinner, then nothing;
  * "find bob" in Contacts with any dialog open recursed until the stack overflowed (no search box -> hand
    "Find bob" back to the router -> the router sent it straight back);
  * "create a new addressbook called Work" opened a New contact named "addressbook called";
  * a dialog with a PASSWORD field open on the page (another app's -- the vault's) was read out to the model as
    this window's text;
  * "Continue writing" built the new body from the note as it was when the AI was ASKED, so a line typed while
    it worked was thrown away.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY
from tests.client.test_window_ai_timeline_and_notes_full_app import ASK, CLICK, NOTE_BODY, OPEN_PANEL, STUB_AI

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


def _cap():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import capture_window_ai_fixtures as cap
    return cap


@CHROME
def test_a_direct_tools_answer_is_on_screen():
    got = {}
    canned = {"window_calc:": {"calc": {"expression": "84.5*15/100", "what": "15% of 84.50"}}}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('calculator');true")
        await b.until("!!window.PCCalc && !!document.querySelector('.calc-screen')")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        await b.js(OPEN_PANEL % json.dumps("calculator"))
        await b.js(ASK % json.dumps("what is 15% of 84.50"))
        await b.until("!!document.querySelector('.osw-ai-panel .osw-ai-calc')")
        got["visible"] = await b.js("(()=>{const a=document.querySelector('.osw-ai-panel .osw-ai-answer');"
                                    "return !!a && a.getClientRects().length>0 && getComputedStyle(a).display!=='none'})()")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["visible"], "the calculation came back into a hidden answer box"


@CHROME
def test_contacts_find_with_a_dialog_open_and_an_addressbook_request_go_to_the_agent():
    got = {}
    cap = _cap()
    canned = {"window_steps:": {"answer": "ok", "tasks": [], "steps": []}}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(cap.CONTACTS)
        await b.js("__PC.switchView('contacts');true")
        await b.until("!!window.PCContacts && document.body.innerText.includes('Alice Jones')")
        await b.js("window.__errs=[]; window.addEventListener('error',e=>__errs.push(String(e.message)));true")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        # A dialog open on the page hides the window's search box (the agent works inside the dialog).
        await b.js("(()=>{const m=document.createElement('div');m.className='modal-bg';m.innerHTML='<div class=modal><p>Some dialog</p></div>';"
                   "m.style.cssText='position:fixed;inset:0;z-index:5';document.body.appendChild(m);window.__dlg=m;})();true")
        await b.js(OPEN_PANEL % json.dumps("contacts"))
        await b.js(ASK % json.dumps("find bob"))
        for _ in range(60):
            if await b.js("__aiSent.length>0"):
                break
            await asyncio.sleep(.1)
        got["find"] = await b.js("__aiSent.map(x=>x.action)")
        got["errs"] = await b.js("__errs.slice()")
        await b.js("__dlg.remove(); document.querySelector('.osw-ai-panel [data-ai-dismiss]').click(); __aiSent.length=0; true")
        await b.js(OPEN_PANEL % json.dumps("contacts"))
        await b.js(ASK % json.dumps("create a new addressbook called Work"))
        for _ in range(60):
            if await b.js("__aiSent.length>0"):
                break
            await asyncio.sleep(.1)
        got["book"] = await b.js("__aiSent.map(x=>x.action)")
        got["form"] = await b.js("!!document.getElementById('cc-given')")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["find"] == ["window_steps"], ("find with a dialog open did not reach the agent", got)
    assert not any("call stack" in e.lower() for e in got["errs"]), got["errs"]
    assert got["book"] == ["window_steps"] and not got["form"], ("an addressbook request opened a New contact", got)


@CHROME
def test_a_password_dialog_is_never_read_out_to_the_model():
    got = {}
    cap = _cap()
    canned = {"window_steps:": {"answer": "ok", "tasks": [], "steps": []}}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(cap.CONTACTS)
        await b.js("__PC.switchView('contacts');true")
        await b.until("!!window.PCContacts && document.body.innerText.includes('Alice Jones')")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        await b.js("(()=>{const m=document.createElement('div');m.className='modal-bg';"
                   "m.innerHTML='<div class=modal><p>Bank login VAULT-SECRET-123</p><input type=password value=hunter2></div>';"
                   "m.style.cssText='position:fixed;inset:0;z-index:5';document.body.appendChild(m);})();true")
        await b.js(OPEN_PANEL % json.dumps("contacts"))
        await b.js(ASK % json.dumps("what is on this screen"))
        for _ in range(60):
            if await b.js("__aiSent.length>0"):
                break
            await asyncio.sleep(.1)
        got["sent"] = await b.js("JSON.stringify(__aiSent)")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["sent"] and "window_steps" in got["sent"], got
    assert "VAULT-SECRET-123" not in got["sent"] and "hunter2" not in got["sent"], "a password dialog was sent to the model"


@CHROME
def test_continue_writing_keeps_what_was_typed_while_it_worked():
    got = {}
    canned = {"window_note:continue": {"note": {"kind": "continue", "append": "Book the night train."}}}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('notes');true")
        await b.until("!!(window.PCNotes && PCNotes.save) && !!document.querySelector('.nt-wrap')")
        await b.js("(async()=>{const r=await PCNotes.save({title:'Trip ideas',body:%s});await PCNotes.select(r.id);window.__nid=r.id;})()" % json.dumps(NOTE_BODY))
        await b.until("!!document.querySelector('.nt-editor') && PCNotes.current() && PCNotes.current().id===__nid")
        await b.js(STUB_AI + "(%s);true" % json.dumps(canned))
        await b.js(OPEN_PANEL % json.dumps("notes"))
        await b.js(CLICK % ("[data-ai-action]", json.dumps("Continue")))
        await b.until("!!document.querySelector('.osw-ai-panel [data-apply]')")
        # The person kept typing while the AI worked.
        await b.js("(async()=>{await PCNotes.update(__nid,{body:%s});})()" % json.dumps(NOTE_BODY + "\nTyped while it worked"))
        await asyncio.sleep(.5)
        await b.js("document.querySelector('.osw-ai-panel [data-apply]').click();true")
        for _ in range(100):
            if await b.js("PCNotes.current().body.includes('night train')"):
                break
            await asyncio.sleep(.1)
        got["body"] = await b.js("PCNotes.current().body")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    assert "night train" in got["body"], got
    assert "Typed while it worked" in got["body"], ("the line typed while the AI worked was thrown away", got["body"])
