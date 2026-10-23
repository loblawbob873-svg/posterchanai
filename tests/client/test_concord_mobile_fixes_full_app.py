"""Concord on a phone, 2026-10-21 reports from the APK, each against the real bundled client and a fixture relay.

* "username completion/tagging functionality missing from concord edit modal": the edit box offers the @-picker,
  a tap inserts the name, and the saved edit carries the mention the way a new message does (`@npub1…`).
* "opening concord post rooms, the thread bar looks out of place": a chat thread's bar reads "#channel" and
  "Thread", never a raw key.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM, CONTROLS
from tests.client.test_concord_forum_full_app import wait_for, type_into

NO_OS = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
GENERAL = ("(async()=>{const ch=PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").channels.find(c=>c.name==='general');"
           "const wraps=__relayStore.filter(e=>ch.streamPubkeys.includes(e.pubkey));"
           "return (await PosterCordReader.inspectChat(__cordBundle," + CONTROLS + ",ch.id,wraps)).messages.map(m=>[m.kind,m.text]);})()")


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_edit_mentions_and_the_thread_bar(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        minted = await b.js(MINT.replace('NAME', json.dumps('TV shows')))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('.cc-channel[data-cc-channel=\"general\"]')")
        await b.js("document.querySelector('.cc-channel[data-cc-channel=\"general\"]').click()")
        await type_into(b, '#cc-input', 'Season finale tonight')
        await b.js("document.querySelector('#cc-send').click()")
        await wait_for(b, GENERAL + ".then(m=>m.some(x=>x[1]==='Season finale tonight'))", 30)
        await wait_for(b, "[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('Season finale tonight')&&m.querySelector('[data-cc-edit]'))", 30)
        # Edit happens IN THE CHAT BAR, like other chat apps: whatever was being typed waits, the bar holds the
        # message, Enter saves. A repaint (a message arriving) must not lose it -- the modal it replaced did.
        await type_into(b, '#cc-input', 'half-typed draft')
        await b.js("[...document.querySelectorAll('.cc-message')].find(m=>m.textContent.includes('Season finale tonight')).querySelector('[data-cc-edit]').click()")
        await wait_for(b, "!!document.querySelector('#cc-reply.cc-editing') && document.querySelector('#cc-input').value==='Season finale tonight'")
        assert not await b.js("!!document.querySelector('.cc-edit-text')"), 'the edit modal is back'
        assert await b.js("document.querySelector('#cc-send').textContent.trim()") == 'Save'
        await b.js("PCConcord.render()")
        assert await b.js("!!document.querySelector('#cc-reply.cc-editing') && document.querySelector('#cc-input').value==='Season finale tonight'"), 'a repaint dropped the edit'
        # Esc cancels and gives the draft back; Edit again resumes.
        await b.js("document.querySelector('#cc-input').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        await wait_for(b, "!document.querySelector('#cc-reply.cc-editing') && document.querySelector('#cc-input').value==='half-typed draft'")
        await b.js("[...document.querySelectorAll('.cc-message')].find(m=>m.textContent.includes('Season finale tonight')).querySelector('[data-cc-edit]').click()")
        await wait_for(b, "document.querySelector('#cc-input').value==='Season finale tonight'")
        await b.js("(()=>{const t=document.querySelector('#cc-input');t.focus();t.setSelectionRange(t.value.length,t.value.length);"
                   "t.setRangeText(' with @',t.value.length,t.value.length,'end');t.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await wait_for(b, "document.querySelectorAll('.cc-mentions .cc-mention-opt').length>0")
        await b.js("(()=>{const r=document.querySelector('.cc-mentions .cc-mention-opt');const o={bubbles:true,pointerId:1,clientX:5,clientY:5};"
                   "r.dispatchEvent(new PointerEvent('pointerdown',o));r.dispatchEvent(new PointerEvent('pointerup',o));})()")
        assert await b.js("/ with @\\S+ $/.test(document.querySelector('#cc-input').value)"), await b.js("document.querySelector('#cc-input').value")
        await b.js("document.querySelector('#cc-input').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}))")
        await wait_for(b, "!document.querySelector('#cc-reply.cc-editing') && document.querySelector('#cc-input').value==='half-typed draft'")
        await b.js("document.querySelector('#cc-input').value='';document.querySelector('#cc-input').dispatchEvent(new Event('input',{bubbles:true}))")
        npub = await b.js("NostrTools.nip19.npubEncode(__PC.me().pubkey)")
        await wait_for(b, GENERAL + ".then(m=>m.some(x=>x[1]==='Season finale tonight with @'+" + json.dumps(npub) + "))", 30)
        # The thread bar of a chat thread: where you are and the way back, no raw key.
        await b.js("[...document.querySelectorAll('.cc-message')].find(m=>m.textContent.includes('Season finale tonight')).querySelector('[data-cc-reply]').click()")
        await type_into(b, '#cc-input', 'Cannot wait')
        await b.js("document.querySelector('#cc-send').click()")
        await wait_for(b, "!!document.querySelector('.cc-ts-open[data-cc-thread]')", 30)
        await b.js("document.querySelector('.cc-ts-open[data-cc-thread]').click()")
        await wait_for(b, "!!document.querySelector('.cc-thread-bar')")
        bar = await b.js("document.querySelector('.cc-thread-bar').textContent")
        assert '#general' in bar and 'Thread' in bar, bar
        assert not __import__('re').search(r'[0-9a-f]{12}', bar), bar
        assert await b.js('document.documentElement.scrollWidth<=innerWidth+1')
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))


SRC = (Path(__file__).resolve().parents[2] / "static/js/client/concord.js").read_text()
APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text()


def test_the_apk_bell_asks_android_not_the_missing_web_api():
    """Android's WebView has no Notifications API: the bell must ask the push plugin's POST_NOTIFICATIONS."""
    body = APP[APP.index("askOsNotify: async () => {"):][:1800]
    assert "_capPlugin('PosterChanPush','notify')" in body and "requestPermissions({permissions:['notifications']})" in body
    assert body.index("requestPermissions") < body.index("if(!window.Notification) return 'unsupported';")


def test_read_marks_follow_the_newest_message_not_this_clock():
    """A sender whose clock runs ahead must not leave an opened channel unread."""
    body = SRC[SRC.index("function markRead(room,name){"):][:2400]
    # The local cursor is the later of this clock and the newest message (a sender running ahead cannot leave an opened
    # channel unread); the SYNCED mark is the newest message seen, so repaints do not republish it (2026-10-22).
    assert "seen=Math.max(seen,Number(m.at)||0)" in body and "const newest=Math.max(Date.now(),seen)" in body
    assert "noteChannelRead(p,room,name||'general',seen)" in body
