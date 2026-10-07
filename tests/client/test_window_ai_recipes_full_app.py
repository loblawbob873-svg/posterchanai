"""✨ buttons that DO something, chosen by what the window can do -- in the shipped panel.

"we need the actions to actually be useful". Each button is a recipe with one outcome (the server builds
the buttons around the text -- tests/test_window_ai_recipes.py); this drives the real panel in popped-out
windows, the way PosterChanOS shows every app, and checks what a person gets:

  * a conversation with a reply box offers "Draft a reply"; pressing it sends the CONVERSATION (not the
    whole window) and the box it will go into, and one press puts the draft in that box -- unsent;
  * a conversation list with no box and no Reply button does not offer a draft it could not deliver;
  * a feed offers Catch me up / Events & to-dos / Write a post…, and "Write a post…" only starts the
    sentence in the ask box;
  * a note with text offers Tidy; one press replaces the note, and Undo puts the original back.
The model's reply is stubbed, shaped exactly as the server's recipe_steps builds it.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


STUB = r"""(()=>{
window.__req=[]; __PC.uiConfirm=async()=>true; window.__copied=[]; __PC.copyValue=v=>{__copied.push(v);return Promise.resolve(true);};
const real=window.fetch;
window.fetch=(url,opts)=>{ if(!String(url).includes('/api/chat-assist')) return real(url,opts);
  const b=JSON.parse(opts.body); __req.push(b); let res;
  if(b.recipe==='draft'){ const ref=b.box_ref||b.reply_ref, t='Yes — I will bring the projector.';
    res={ok:true,answer:t,tasks:[],steps:ref?[{do:'fill',ref,target:b.box_label||'Reply',label:'Put it in the reply box',text:t,on:false}]:[{do:'insert',label:'Put it in the reply box',text:t}]}; }
  else if(b.recipe==='tidy'||b.recipe==='checklist'){ const t='Groceries\n- milk\n- eggs';
    res={ok:true,answer:t,tasks:[],steps:[{do:'fill',ref:b.box_ref,target:b.box_label||'note',label:'Replace the note with this',text:t,on:false}]}; }
  else res={ok:true,answer:'- point',tasks:[],steps:[{do:'note',label:'Save summary to Notes',text:'- point'}]};
  return Promise.resolve(new Response(JSON.stringify(res),{status:200,headers:{'Content-Type':'application/json'}})); };
})()"""

CONVO = r"""(()=>{const f=document.getElementById('feed');const c=document.createElement('div');c.id='conv';
c.innerHTML='<p>'+('Dana: hey! are you still coming to the meetup on Saturday? 6pm at the usual place. Can you bring the projector? ').repeat(3)+'</p>';
const t=document.createElement('textarea');t.id='reply-box';t.setAttribute('aria-label','Message Dana');t.style.cssText='display:block;width:300px;height:40px';
c.appendChild(t);f.appendChild(c);t.focus();return true;})()"""

NOTE = r"""(()=>{const f=document.getElementById('feed');const t=document.createElement('textarea');t.id='note-body';t.setAttribute('aria-label','Note');
t.value='milk eggs\ncall mom';t.style.cssText='display:block;width:300px;height:80px';f.appendChild(t);t.focus();return true;})()"""

BUTTONS = "[...document.querySelectorAll('.osw-ai-panel [data-ai-action] b')].map(x=>x.textContent)"


async def _panel(b, view, label, setup=""):
    await desktop.login(b)
    await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 760, "deviceScaleFactor": 1, "mobile": False})
    await b.until("!!(window.PCOSWin && PCOSWin.isWindow())")
    await b.js("document.getElementById('pc-oswin-chrome') || PCOSWin.adopt({view:'%s', label:'%s'})" % (view, label))
    await b.until("!!document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]')")
    await b.js(STUB)
    if setup:
        await b.js(setup)
    await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click()")
    await b.until("!!document.querySelector('.osw-ai-panel [data-ai-action]')")


async def _press(b, label):
    await b.js("[...document.querySelectorAll('.osw-ai-panel [data-ai-action]')].find(x=>x.querySelector('b').textContent===%s).click()" % json.dumps(label))


CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@CHROME
def test_a_conversation_gets_a_draft_that_lands_in_its_own_reply_box_unsent():
    got = {}

    async def check(b):
        await _panel(b, "messages", "Messages", CONVO)
        got["buttons"] = await b.js(BUTTONS)
        await _press(b, "Draft a reply")
        await b.until("__req.length===1 && !!document.querySelector('.osw-ai-step [data-act]')")
        got["req"] = await b.js("__req[0]")
        got["boxRefIsTheBox"] = await b.js("(()=>{const r=__req[0];return r.box_ref>0})()")
        await b.js("document.querySelector('.osw-ai-step [data-act]').click()")
        await b.until("document.getElementById('reply-box').value.length>0")
        got["box"] = await b.js("document.getElementById('reply-box').value")
        got["done"] = await b.js("document.querySelector('.osw-ai-step').classList.contains('done')")

    asyncio.run(desktop.with_browser("online", "?pcwin=messages", check))
    assert got["buttons"][:3] == ["Draft a reply", "Catch me up", "To-dos & dates"], got["buttons"]
    r = got["req"]
    assert r["action"] == "window_recipe" and r["recipe"] == "draft", r
    assert got["boxRefIsTheBox"] and r["box_label"] == "Message Dana", r
    assert "bring the projector" in r["windows"][0]["selection"], ("the draft was not given the conversation", r["windows"][0])
    assert got["box"] == "Yes — I will bring the projector." and got["done"], got


@CHROME
def test_a_list_with_nowhere_to_reply_offers_no_draft():
    got = {}

    async def check(b):
        await _panel(b, "messages", "Messages")
        got["buttons"] = await b.js(BUTTONS)

    asyncio.run(desktop.with_browser("online", "?pcwin=messages", check))
    assert "Draft a reply" not in got["buttons"] and "Catch me up" in got["buttons"], got


@CHROME
def test_a_feed_catches_you_up_and_write_a_post_only_starts_the_sentence():
    got = {}

    async def check(b):
        await _panel(b, "global", "Social")
        got["buttons"] = await b.js(BUTTONS)
        await _press(b, "Write a post…")
        await asyncio.sleep(.2)
        got["ask"] = await b.js("({v:document.querySelector('.osw-ai-panel textarea').value, calls:__req.length})")
        await _press(b, "Catch me up")
        await b.until("__req.length===1 && !!document.querySelector('.osw-ai-step [data-go]')")
        got["req"] = await b.js("({action:__req[0].action, recipe:__req[0].recipe})")
        got["save"] = await b.js("document.querySelector('.osw-ai-step [data-go]').textContent")

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))
    assert got["buttons"] == ["Catch me up", "Events & to-dos", "Write a post…"], got["buttons"]
    assert got["ask"] == {"v": "Write a post about ", "calls": 0}, got["ask"]
    assert got["req"] == {"action": "window_recipe", "recipe": "summary"}, got["req"]
    assert got["save"] == "Save to Notes", got["save"]


@CHROME
def test_a_note_is_tidied_in_place_and_undo_puts_it_back():
    got = {}

    async def check(b):
        # The TEXT-BOX path: a note-like window whose notebook module is not loaded. (The Notes app itself
        # reads the notebook -- tests/client/test_window_ai_timeline_and_notes_full_app.py.)
        await _panel(b, "notes", "Notes", "window.PCNotes=undefined;" + NOTE)
        got["buttons"] = await b.js(BUTTONS)
        await _press(b, "Tidy this note")
        await b.until("__req.length===1 && !!document.querySelector('.osw-ai-step [data-act]')")
        got["sent"] = await b.js("({text:__req[0].text, ref:__req[0].box_ref>0})")
        await b.js("document.querySelector('.osw-ai-step [data-act]').click()")
        await b.until("document.getElementById('note-body').value.startsWith('Groceries')")
        await b.js("document.querySelector('.osw-ai-panel [data-ai-undo]').click()")
        await asyncio.sleep(.2)
        got["after_undo"] = await b.js("document.getElementById('note-body').value")

    asyncio.run(desktop.with_browser("online", "?pcwin=notes", check))
    assert got["buttons"][:2] == ["Tidy this note", "Make it a checklist"], got["buttons"]
    assert got["sent"] == {"text": "milk eggs\ncall mom", "ref": True}, got["sent"]
    assert got["after_undo"] == "milk eggs\ncall mom", got
