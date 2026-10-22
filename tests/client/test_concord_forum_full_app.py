"""FORUM CHANNELS (CORD-03 §2 `view: "forum"`, Armada's Forum channel), end to end.

Reported by a member: "I don't see forum channels at all." In Armada a channel whose metadata says
`view: "forum"` opens as a list of titled posts; a post is a kind-9 message carrying a NIP-14
`["subject", <title>]` tag and its comments are kind-1111 thread replies (Armada src/concord/lib/
forum.ts + channelView.ts). Nothing here invents a kind: the channel is an ordinary CORD-03 channel
and a client without forum support still reads the posts as messages with threads.

Real minted community, real reader/writer and signer, real bundled DOM and pointer input; the relay
is an in-page fixture that honours filters (tests/client/concord_fixture_relay.py).
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM, CONTROLS
from tests.client.test_concord_channel_controls_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def wait_for(b, expression, seconds=20):
    for _ in range(int(seconds * 10)):
        if await b.js(expression):
            return
        await asyncio.sleep(.1)
    raise AssertionError({'waiting': expression, 'body': await b.js('document.body.innerText.slice(-1500)')})


async def type_into(b, selector, text):
    await b.js('(()=>{const el=document.querySelector(' + json.dumps(selector) + ');el.focus();el.value=' + json.dumps(text)
               + ';el.dispatchEvent(new Event("input",{bubbles:true}));})()')


def chat_of(channel_expr):
    """The messages of a channel as the READER decodes them from what reached the relay."""
    return ('(async()=>{const ch=' + channel_expr + ';const wraps=__relayStore.filter(e=>ch.streamPubkeys.includes(e.pubkey));'
            'return (await PosterCordReader.inspectChat(__cordBundle,' + CONTROLS + ',ch.id,wraps)).messages;})()')


IDEAS = "PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").channels.find(c=>c.name==='ideas')"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_forum_channel_create_post_reply(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        minted = await b.js(MINT.replace('NAME', json.dumps('Forum fixture')))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('#cc-add-channel')")

        # 1. The New channel dialog offers the CORD-03 views, Text and Forum, plus Armada's repository channel
        #    (a Text channel created with a git repository attached -- concord-git.js).
        await click(b, '#cc-add-channel')
        await b.until("document.querySelector('.uiconfirm-msg')?.textContent==='Name the new channel'")
        assert await b.js("[...document.querySelectorAll('[data-cc-view]')].map(x=>x.dataset.ccView).join()") == 'chat,forum,git'
        await click(b, '[data-cc-view="forum"]')
        assert await b.js("document.querySelector('[data-cc-view=\"forum\"]').getAttribute('aria-checked')") == 'true'
        await type_into(b, '.uiprompt-in', 'ideas')
        await click(b, '.uiconfirm [data-uc="1"]')
        await wait_for(b, IDEAS + "?.view==='forum'")
        # 2. It is listed as a forum, with its own glyph.
        await wait_for(b, "!!document.querySelector('.cc-channel[data-cc-channel=\"ideas\"] .cc-channel-kind[data-view=\"forum\"]')")
        assert await b.js("document.querySelector('.cc-channel[data-cc-channel=\"general\"] .cc-channel-kind').dataset.view") == 'chat'
        # A metadata pass that started before the create can briefly repaint the list without the
        # new channel (pre-existing, independent of the channel's type); let one 4 s tick settle.
        await asyncio.sleep(4.5)
        await wait_for(b, "!!document.querySelector('.cc-channel[data-cc-channel=\"ideas\"] .cc-channel-kind[data-view=\"forum\"]')")
        # 3. Opening it shows the post list, not a chat composer.
        await click(b, '.cc-channel[data-cc-channel="ideas"]')
        await wait_for(b, "!!document.querySelector('.cc-forum #cc-forum-new')")
        assert await b.js("getComputedStyle(document.querySelector('.cc-compose')).display") == 'none'

        async def new_post(title, body):
            await click(b, '#cc-forum-new')
            await b.until("document.querySelector('.uiconfirm-msg')?.textContent==='New post'")
            await type_into(b, '#cc-post-title', title)
            await type_into(b, '#cc-post-body', body)
            await click(b, '.uiconfirm [data-uc="1"]')
            await wait_for(b, "[...document.querySelectorAll('.cc-forum-post .cc-forum-title')].some(x=>x.textContent===" + json.dumps(title) + ")")

        await new_post('Roadmap', 'What should we build next?')
        await new_post('Bugs', 'Report problems here.')
        # Both posts reach the relay (delivery runs behind the optimistic card).
        await wait_for(b, chat_of(IDEAS) + ".then(m=>m.filter(x=>x.kind===9).length===2)")
        posts = await b.js(chat_of(IDEAS))
        roots = {m['tags'][[t[0] for t in m['tags']].index('subject')][1]: m for m in posts if m['kind'] == 9 and any(t[0] == 'subject' for t in m['tags'])}
        assert set(roots) == {'Roadmap', 'Bugs'}, posts
        assert roots['Roadmap']['text'] == 'What should we build next?'
        # The card shows title, the first post and a reply count.
        card = "document.querySelector('.cc-forum-post[data-cc-thread=\"" + roots['Bugs']['id'] + "\"]')"
        assert await b.js(card + ".querySelector('.cc-forum-excerpt').textContent") == 'Report problems here.'
        assert await b.js(card + ".querySelector('.cc-forum-replies').textContent") == '0 replies'

        # 4. Open "Bugs", reply, and the reply lands in THAT thread.
        await click(b, '.cc-forum-post[data-cc-thread="' + roots['Bugs']['id'] + '"]')
        await wait_for(b, "document.querySelector('.cc-forum-subject')?.textContent==='Bugs'")
        # The bar above an open post names the POST and the way back -- not "Thread" and a raw key.
        assert await b.js("document.querySelector('.cc-thread-bar .cc-thread-what b').textContent") == 'Bugs'
        assert await b.js("document.querySelector('#cc-thread-back').textContent.includes('All posts')")
        assert await b.js("getComputedStyle(document.querySelector('.cc-compose')).display") != 'none'
        await type_into(b, '#cc-input', 'The login page is slow')
        await click(b, '#cc-send')
        await wait_for(b, chat_of(IDEAS) + ".then(m=>m.some(x=>x.kind===1111))")
        replies = [m for m in await b.js(chat_of(IDEAS)) if m['kind'] == 1111]
        assert len(replies) == 1 and replies[0]['text'] == 'The login page is slow'
        tags = replies[0]['tags']
        assert ['K', '9'] in tags
        assert any(t[0] == 'E' and t[1] == roots['Bugs']['id'] for t in tags), tags
        assert any(t[0] == 'e' and t[1] == roots['Bugs']['id'] for t in tags), tags
        # 5. Back at the list: the right post counts the reply, the other does not.
        await click(b, '#cc-thread-back')
        await wait_for(b, card + "?.querySelector('.cc-forum-replies').textContent==='1 reply'")
        assert await b.js("document.querySelector('.cc-forum-post[data-cc-thread=\"" + roots['Roadmap']['id'] + "\"] .cc-forum-replies').textContent") == '0 replies'
        # Most recently active first.
        assert await b.js("document.querySelector('.cc-forum-post .cc-forum-title').textContent") == 'Bugs'
        # 6. A rename keeps the channel a forum (the edition carries `view` through).
        await b.js(r'''(async()=>{const ch=''' + IDEAS + r''';const made=await PosterCordReader.createChannelEditWrap(__cordBundle,''' + CONTROLS + r''',ch.id,{name:'ideas-2'},__PC.me().pubkey,__PC.signTemplate);__relaySeed([made.wrap]);})()''')
        assert await b.js("PosterCordReader.inspectControl(__cordBundle," + CONTROLS + ").channels.find(c=>c.name==='ideas-2').view") == 'forum'
        # Nothing scrolls sideways on a phone.
        assert await b.js('document.documentElement.scrollWidth<=innerWidth+1')
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
