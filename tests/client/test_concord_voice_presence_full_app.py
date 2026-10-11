"""VOICE IN A CONCORD CHANNEL: who is connected is shown on the channel row, joining is one tap,
and two members see each other (CORD-07).

Reported by a member: "I don't see voice channels." CORD-07 says every Channel is callable — "there
is no separate 'voice Channel' type" — and Armada draws exactly that: a channel with a live call
swaps its # for a speaker and lists the people in it under the row (ConcordPage.tsx `occupied`,
ChannelGlyph.tsx). Presence is the signed ephemeral kind-23313 rumor ("joined"/"left", `identity`
and `broker` tags) inside a 21059 wrap on the channel's own stream.

TWO REAL BROWSERS, two accounts, one minted community. Each page's relay is an in-page fixture and
the test pumps every event one page publishes into the other, ephemeral kinds included — so what B
shows is what B decrypted from A's wrap with B's own reader. The SFU half (LiveKit token + media) is
the isolated fixture the existing call tests use: there is no CORD-07 broker in CI, and audio
between the two pages needs one (see the module report).
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM, pump
from tests.client.test_concord_channel_controls_full_app import click

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def wait_for(b, expression, seconds=20):
    for _ in range(int(seconds * 10)):
        if await b.js(expression):
            return
        await asyncio.sleep(.1)
    raise AssertionError({'waiting': expression, 'voice': await b.js('JSON.stringify(PCConcord.__voiceWatch&&PCConcord.__voiceWatch())'),
                          'rows': await b.js("[...document.querySelectorAll('.cc-voice-people')].map(x=>x.outerHTML.slice(0,300))"),
                          'body': await b.js('document.body.innerText.slice(-600)')})


async def login_as(b, fill):
    await b.until("document.body.classList.contains('guest')")
    await b.js("(()=>{const key=new Uint8Array(32).fill(" + str(fill) + ");document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
    await b.until('!!__PC.me()')


# The SFU boundary, as in test_concord_call_full_app.py: the token, LiveKit room and E2EE worker
# are fixtures; everything that decides WHO is in the call (signed presence) is real.
SFU = r'''(()=>{
  window.__callLog=[];
  window.Worker=class{constructor(){}terminate(){__callLog.push('terminate')}};
  const provider=class{constructor(){}onSetEncryptionKey(){}};
  const room=class{constructor(){this.remoteParticipants=new Map();this.localParticipant={identity:'sfu-'+__PC.me().pubkey.slice(0,8),
    setMicrophoneEnabled:async v=>__callLog.push('mic:'+v),setCameraEnabled:async()=>{},setScreenShareEnabled:async()=>{}}}
    on(){return this}async setE2EEEnabled(){}async connect(){__callLog.push('connect')}async disconnect(){__callLog.push('disconnect')}};
  window.LivekitClient={BaseKeyProvider:provider,Room:room,RoomEvent:{},isE2EESupported:()=>true};
  window.PCCordVoice={...PCCordVoice,token:async()=>({identity:'sfu-'+__PC.me().pubkey.slice(0,8),token:'fixture',url:'wss://sfu.invalid',broker:'https://sfu.invalid'})};
})()'''

PEOPLE = "(()=>[...document.querySelectorAll('.cc-voice-people[data-cc-voice-for=\"'+__generalId+'\"] [data-pk]')].map(x=>x.dataset.pk).sort())()"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_two_members_see_each_other_in_a_channel_call(width):
    shared = {}
    ready = {'a': asyncio.Event(), 'b': asyncio.Event()}
    pages = {}
    done = asyncio.Event()

    async def boot(b, fill):
        # The pump talks to this page while the test does: one CDP command at a time per socket.
        lock, call = asyncio.Lock(), b.call

        async def serial(*args, **kwargs):
            async with lock:
                return await call(*args, **kwargs)
        b.call = serial
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await login_as(b, fill)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js((ROOT / 'static/js/client/cord-voice.js').read_text())
        await b.js((ROOT / 'static/js/client/cord-call.js').read_text())
        await b.js(SFU)
        await b.js(INSTALL)

    async def side_a(b):
        await boot(b, 1)
        minted = await b.js(MINT.replace('NAME', json.dumps('Voice fixture')))
        shared['room'] = minted['room']
        shared['events'] = minted['events']
        pages['a'] = b
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.js("window.__generalId=__cordRoom.channels.find(c=>c.name==='general').id")
        ready['a'].set()
        await ready['b'].wait()
        a_pk, b_pk = await b.js('__PC.me().pubkey'), shared['b_pk']
        # The row is callable and shows nobody yet.
        await wait_for(b, "!!document.querySelector('[data-cc-voice-join=\"'+__generalId+'\"]')")
        assert await b.js(PEOPLE) == []
        # One tap joins: the row opens the call and, once this page has listened for an existing
        # call (CORD-07 §5), connects without a second tap.
        await click(b, '[data-cc-voice-join="' + await b.js('__generalId') + '"]')
        await wait_for(b, "/Connected/.test(document.querySelector('.pc-cord-call [role=status]')?.textContent||'')", 45)
        await wait_for(b, PEOPLE + ".includes(" + json.dumps(a_pk) + ")")
        shared['a_joined'] = True
        # B joins too, and A sees B.
        await wait_for(b, PEOPLE + ".length===2", 60)
        assert await b.js(PEOPLE) == sorted([a_pk, b_pk])
        assert await b.js("document.querySelector('.cc-channel[data-cc-channel=\"general\"] .cc-channel-kind').dataset.occupied") == 'true'
        # Leaving takes A off B's row (checked on B's side).
        await click(b, '[data-call=close]')
        await wait_for(b, "!document.querySelector('.pc-cord-call')")
        shared['a_left'] = True
        await done.wait()

    async def side_b(b):
        await boot(b, 2)
        await ready['a'].wait()
        pages['b'] = b
        shared['b_pk'] = await b.js('__PC.me().pubkey')
        await b.js("__relaySeed(" + json.dumps(shared['events']) + ")")
        await b.js(OPEN_ROOM + '(' + json.dumps(shared['room']) + ')')
        await b.js("window.__generalId=__cordRoom.channels.find(c=>c.name==='general').id")
        ready['b'].set()
        a_pk = await pages['a'].js('__PC.me().pubkey')
        # A's presence reaches B over the relay and is decrypted by B's reader.
        await wait_for(b, PEOPLE + ".includes(" + json.dumps(a_pk) + ")", 60)
        assert await b.js("document.querySelector('.cc-channel[data-cc-channel=\"general\"] .cc-channel-kind').dataset.occupied") == 'true'
        # Interop: the wrap A published parses with the CORD reader and folds with CORD-07's rules.
        presence = await b.js(r'''(async()=>{const ctl=__relayStore.filter(e=>e.kind===1059);
          const rows=await PosterCordReader.inspectVoicePresence(__cordBundle,ctl,__generalId,__relayEphemeral);
          return {rows,folded:PCCordVoice.fold(rows)};})()''')
        mine = [r for r in presence['rows'] if r['pubkey'] == a_pk and r['content'] == 'joined']
        assert mine, presence
        assert any(t[0] == 'identity' for t in mine[-1]['tags']) and ['broker', 'https://sfu.invalid'] in mine[-1]['tags']
        general_id = await b.js('__generalId')
        assert ['channel', general_id] in mine[-1]['tags']
        assert [p['pubkey'] for p in presence['folded'] if p['verified']] == [a_pk]
        # B joins (the call is occupied, so no listening wait), A sees B (checked on A's side).
        await click(b, '[data-cc-voice-join="' + await b.js('__generalId') + '"]')
        await wait_for(b, "/Connected/.test(document.querySelector('.pc-cord-call [role=status]')?.textContent||'')", 20)
        await wait_for(b, PEOPLE + ".length===2", 30)
        # A leaves; B's row drops A and keeps B.
        while not shared.get('a_left'):
            await asyncio.sleep(.2)
        await wait_for(b, PEOPLE + "===undefined||JSON.stringify(" + PEOPLE + ")===" + json.dumps(json.dumps([shared['b_pk']])), 30)
        assert await b.js(PEOPLE) == [shared['b_pk']]
        await click(b, '[data-call=close]')
        await wait_for(b, PEOPLE + ".length===0", 30)
        done.set()

    async def main():
        stop = asyncio.Event()

        async def pumping():
            while not ('a' in pages and 'b' in pages):
                await asyncio.sleep(.1)
            await pump(pages['a'], pages['b'], stop)

        extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
        pumper = asyncio.create_task(pumping())
        try:
            await asyncio.wait_for(asyncio.gather(desktop.with_browser('online', '', side_a, extra),
                                                  desktop.with_browser('online', '', side_b, extra)), 300)
        finally:
            stop.set()
            pumper.cancel()

    asyncio.run(main())
