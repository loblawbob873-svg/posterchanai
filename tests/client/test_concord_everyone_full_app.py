"""Concord @everyone, as Armada does it: the owner is offered it, picking it writes the literal `@everyone`, and the
message goes out as exactly that -- no `p` tag for anybody, since the token itself is the mention.

Asked 2026-10-27: "it looks like our concord is missing the @everyone tag" / "armada has that I think". Armada's wire
form (src/concord/lib/everyoneMention.ts): CORD reserves MENTION_EVERYONE and defines no tag, so clients send the
literal lowercase token, honoured only from the owner or a holder of that permission. Who RECEIVES it as a mention
(owner / permission / look-alikes like a@everyone.com) is concord_members_runtime.mjs. Real bundled client, a fixture
relay, a phone and a desktop width.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM, CONTROLS
from tests.client.test_concord_forum_full_app import wait_for, type_into

GENERAL = ("(async()=>{const ch=PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").channels.find(c=>c.name==='general');"
           "const wraps=__relayStore.filter(e=>ch.streamPubkeys.includes(e.pubkey));"
           "return (await PosterCordReader.inspectChat(__cordBundle," + CONTROLS + ",ch.id,wraps)).messages"
           ".map(m=>({text:m.text,p:(m.tags||[]).filter(t=>t[0]==='p'||t[0]==='P').length}));})()")
OPTS = "[...document.querySelectorAll('.cc-mentions .cc-mention-opt')].map(o=>o.textContent.trim())"


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_the_owner_picks_everyone_and_it_goes_out_as_the_literal_token(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        minted = await b.js(MINT.replace('NAME', json.dumps('Announcements')))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('.cc-channel[data-cc-channel=\"general\"]')")
        await b.js("document.querySelector('.cc-channel[data-cc-channel=\"general\"]').click()")
        await type_into(b, '#cc-input', 'Stream starts now @ev')
        await wait_for(b, "document.querySelectorAll('.cc-mentions .cc-mention-opt').length>0")
        got["opts"] = await b.js(OPTS)
        got["first_is_everyone"] = await b.js("document.querySelector('.cc-mentions .cc-mention-opt').classList.contains('cc-mention-everyone')")
        await b.js("(()=>{const r=document.querySelector('.cc-mentions .cc-mention-everyone');const o={bubbles:true,pointerId:1,clientX:5,clientY:5};"
                   "r.dispatchEvent(new PointerEvent('pointerdown',o));r.dispatchEvent(new PointerEvent('pointerup',o));})()")
        got["typed"] = await b.js("document.querySelector('#cc-input').value")
        await b.js("document.querySelector('#cc-send').click()")
        await wait_for(b, GENERAL + ".then(m=>m.some(x=>x.text.includes('@everyone')))", 30)
        got["wire"] = await b.js(GENERAL)
        # The row draws @everyone as a mention.
        await wait_for(b, "[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('Stream starts now'))", 30)
        got["painted"] = await b.js("[...document.querySelectorAll('.cc-message')].filter(m=>m.textContent.includes('Stream starts now'))"
                                    ".map(m=>[...m.querySelectorAll('.cc-mention')].map(x=>x.textContent)).flat()")
        got["sideways"] = await b.js("document.documentElement.scrollWidth>innerWidth+1")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser('online', '', check, "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"))
    assert got["first_is_everyone"] and any(o.startswith('📣@everyone') for o in got["opts"]), ("the owner is not offered @everyone", got)
    assert got["typed"] == "Stream starts now @everyone ", ("picking it did not write the literal token", got)
    sent = [m for m in got["wire"] if 'Stream starts now' in m["text"]]
    assert sent and sent[0]["text"] == "Stream starts now @everyone", ("the wire text is not the literal @everyone", got)
    assert sent[0]["p"] == 0, ("@everyone tagged somebody: the token is the mention", got)
    assert "@everyone" in got["painted"], ("the message does not draw @everyone as a mention", got)
    assert not got["sideways"] and not got["errors"], got
