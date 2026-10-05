"""Voice, video and group calls, end to end: real browsers calling each other over a real relay.

Every other call test stops at a boundary — the module boots (test_calls_module_boots_full_app), the
server decides which frame may ring a phone (test_call_ring_filter), Concord's SFU is a stub
(test_concord_call_*). None of them ever had two people on a call, so nothing checked the part a user
actually experiences: that pressing Call makes the OTHER person's screen ring, that Answer gets sound
across, that End ends it on both sides, that a group call connects everyone to everyone.

This does. Each participant is its own headless Chrome (own profile, own key, own sessionStorage —
i.e. its own device), with Chrome's fake camera and microphone, running the SHIPPED client
(templates/client.html + static/js/client/*). Between them is a real WebSocket relay (a few lines of
NIP-01 below), so the kind-25050 signalling is signed, NIP-44 encrypted, published, fanned out and
decrypted exactly as in production. WebRTC is real: the assertions read RTCPeerConnection stats, so
"connected" means audio/video BYTES ARRIVED, not that a status line says so.

Only the instance's HTTP is a fixture (/client/config names the test relay; TURN credentials are
empty, so ICE uses host candidates on loopback).
"""
import asyncio
import contextlib
import json
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, FIRST_RUN_DONE

CHROME = '/opt/google/chrome/chrome'
pytestmark = pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')


# ---------------------------------------------------------------------------------------------------
# A real relay: NIP-01 REQ/EVENT/CLOSE, filters on ids/authors/kinds/#p/since, ephemeral kinds fanned
# out and never stored (that is the property calls depend on — a missed invite is gone for good).
# ---------------------------------------------------------------------------------------------------
def _matches(f, ev):
    if f.get('ids') and ev['id'] not in f['ids']: return False
    if f.get('authors') and ev['pubkey'] not in f['authors']: return False
    if f.get('kinds') and ev['kind'] not in f['kinds']: return False
    if f.get('since') and ev['created_at'] < f['since']: return False
    if f.get('until') and ev['created_at'] > f['until']: return False
    for k, want in f.items():
        if k.startswith('#') and want:
            have = {t[1] for t in ev.get('tags', []) if len(t) > 1 and t[0] == k[1:]}
            if not have & set(want): return False
    return True


class Relay:
    def __init__(self):
        self.stored, self.subs, self.log = [], {}, []   # subs: ws -> {sub_id: [filters]}

    async def handler(self, ws):
        self.subs[ws] = {}
        try:
            async for raw in ws:
                try: m = json.loads(raw)
                except ValueError: continue
                if m[0] == 'REQ':
                    self.subs[ws][m[1]] = m[2:]
                    for ev in self.stored:
                        if any(_matches(f, ev) for f in m[2:]): await ws.send(json.dumps(['EVENT', m[1], ev]))
                    await ws.send(json.dumps(['EOSE', m[1]]))
                elif m[0] == 'CLOSE':
                    self.subs[ws].pop(m[1], None)
                elif m[0] == 'EVENT':
                    ev = m[1]; self.log.append(ev)
                    if not 20000 <= ev['kind'] < 30000: self.stored.append(ev)
                    await ws.send(json.dumps(['OK', ev['id'], True, '']))
                    for other, subs in list(self.subs.items()):
                        for sid, filters in list(subs.items()):
                            if any(_matches(f, ev) for f in filters):
                                try: await other.send(json.dumps(['EVENT', sid, ev]))
                                except websockets.ConnectionClosed: pass
        finally:
            self.subs.pop(ws, None)

    def frames(self, kind=25050):
        return [e for e in self.log if e['kind'] == kind]


def _init(relay_url):
    # The instance: its relay is the test relay, calls get no TURN (host candidates on loopback).
    # RTCPeerConnection is wrapped ONLY to keep a list of them, so a test can read real getStats().
    return FIRST_RUN_DONE + r'''
window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message||e)));
const _f=window.fetch.bind(window);
window.fetch=async function(url,opts={}){
  const u=String(url);
  if(new URL(u,location.href).origin===location.origin && (u.includes('/static/')||/\/client\/[^?]+\.js/.test(u)))return _f(url,opts);
  let d={};
  if(u.includes('/client/config'))d={nostr_only:false,relay_url:RELAY,relays:[RELAY]};
  if(u.includes('/auth/nostr-login'))d={access_token:'fixture-token',user:{id:1,can_ai:false,can_blossom:false,is_admin:false}};
  if(u.includes('/api/calls/turn-credentials'))d={iceServers:[],defaultVideo:false};
  return new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}});
};
window.__pcs=[];
const _PC=window.RTCPeerConnection;
window.RTCPeerConnection=class extends _PC{constructor(...a){super(...a);__pcs.push(this);}};
'''.replace('RELAY', json.dumps(relay_url))


class Person:
    """One device: a headless Chrome logged in with its own key."""
    def __init__(self, b, name): self.b, self.name = b, name

    async def js(self, expr): return await self.b.js(expr)

    async def until(self, expr, timeout=20, what=''):
        loop = asyncio.get_running_loop(); end = loop.time() + timeout
        while loop.time() < end:
            if await self.b.js(expr): return
            await asyncio.sleep(.15)
        state = await self.b.js("({errors:__errors,overlay:(document.querySelector('#call-overlay,#room-overlay')||{}).innerText||null,"
                                "status:(document.querySelector('#call-status')||{}).textContent||null,"
                                "pcs:__pcs.map(p=>p.connectionState),toast:(document.querySelector('.toast')||{}).textContent||null})")
        raise AssertionError(f'{self.name}: waited {timeout}s for {what or expr}; state={state}')

    # -- what the person sees and presses ---------------------------------------------------------
    async def overlay(self):
        return await self.js("(()=>{const o=document.querySelector('#call-overlay');return o?{cls:o.className,"
                             "status:(o.querySelector('#call-status')||{}).textContent||'',"
                             "buttons:[...o.querySelectorAll('.call-act span')].map(s=>s.textContent)}:null})()")

    async def press(self, button_id):
        await self.until(f"!!document.getElementById('{button_id}')", what=f'the {button_id} button')
        await self.js(f"document.getElementById('{button_id}').click()")

    async def call(self, other):
        await self.js("__PC.switchView('calls')")
        await self.until("!!document.querySelector('#call-npub')", what='the Calls screen')
        await self.js(f"document.querySelector('#call-npub').value={json.dumps(other.npub)};document.querySelector('#call-start-btn').click()")

    async def media_in(self, kind='audio'):
        """Bytes of `kind` received across all of this device's live peer connections (real getStats)."""
        return await self.js(f"""(async()=>{{let n=0;for(const pc of __pcs){{if(pc.connectionState==='closed')continue;
            const s=await pc.getStats();s.forEach(r=>{{if(r.type==='inbound-rtp'&&r.kind==='{kind}')n+=r.bytesReceived||0;}});}}return n;}})()""")

    async def receives(self, kind='audio', what=''):
        """Media is FLOWING: the byte count is non-zero and still going up."""
        await self.until(f"(async()=>{{let n=0;for(const pc of __pcs){{if(pc.connectionState==='closed')continue;const s=await pc.getStats();"
                         f"s.forEach(r=>{{if(r.type==='inbound-rtp'&&r.kind==='{kind}')n+=r.bytesReceived||0;}});}}return n>0;}})()",
                         timeout=25, what=what or f'{kind} to arrive')
        first = await self.media_in(kind); await asyncio.sleep(1.0)
        assert await self.media_in(kind) > first, f'{self.name}: {kind} arrived once and then stopped'

    async def live_connections(self):
        return await self.js("__pcs.filter(p=>p.connectionState==='connected').length")

    async def missed(self):
        return await self.js("JSON.parse(localStorage.getItem('pc_missed_calls')||'[]').map(x=>x.pk)")


async def _open(stack, http_port, relay_url, key_byte, name):
    profile = stack.enter_context(tempfile.TemporaryDirectory(prefix='pc-call-', ignore_cleanup_errors=True))
    proc = subprocess.Popen([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=1100,900',
                             '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
                             '--autoplay-policy=no-user-gesture-required',
                             '--disable-features=WebRtcHideLocalIpsWithMdns',
                             '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stack.callback(lambda: (proc.poll() is None) and (proc.kill(), proc.wait(timeout=10)))
    port_file = Path(profile, 'DevToolsActivePort')
    for _ in range(300):
        try:
            port = port_file.read_text().splitlines()[0]
            if port.isdigit(): break
        except (FileNotFoundError, IndexError): pass
        await asyncio.sleep(.1)
    else:
        raise AssertionError(f'{name}: Chrome did not start')
    target = None
    async with httpx.AsyncClient(trust_env=False) as h:
        for _ in range(100):
            try:
                pages = (await h.get(f'http://127.0.0.1:{port}/json', timeout=2)).json()
                target = next((p['webSocketDebuggerUrl'] for p in pages if p.get('type') == 'page'), None)
                if target: break
            except (httpx.HTTPError, ValueError): pass
            await asyncio.sleep(.1)
    ws = await websockets.connect(target, max_size=20_000_000)
    stack.push_async_callback(ws.close)
    b = Browser(ws)
    await b.call('Page.enable')
    await b.call('Network.enable')
    await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
    await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': _init(relay_url)})
    await b.call('Page.navigate', {'url': f'http://127.0.0.1:{http_port}/client'})
    p = Person(b, name)
    await p.until("!!window.__PC && !!window.NostrTools && document.readyState==='complete'", timeout=40, what='the client to boot')
    await p.until("document.body.classList.contains('guest')", timeout=30, what='the login screen')
    await p.js(f"(()=>{{const k=new Uint8Array(32).fill({key_byte});document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(k);"
               "document.querySelector('#btn-nsec-login').click()})()")
    await p.until('!!__PC.me()', timeout=30, what='login')
    p.pubkey = await p.js('__PC.me().pubkey')
    p.npub = await p.js('NostrTools.nip19.npubEncode(__PC.me().pubkey)')
    # Signalling is live once this device's call subscription reached the relay.
    return p


def scenario(*people):
    """Run `check(relay, *persons)` with one browser per (key_byte, name)."""
    def wrap(check):
        async def run():
            relay = Relay()
            server = await websockets.serve(relay.handler, '127.0.0.1', 0)
            relay_url = 'ws://127.0.0.1:%d' % server.sockets[0].getsockname()[1]
            http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                async with contextlib.AsyncExitStack() as stack:
                    ps = await asyncio.gather(*[_open(stack, http.server_port, relay_url, k, n) for k, n in people])
                    # Every device has subscribed for its own call frames before anyone dials.
                    def armed(p):
                        return any(p.pubkey in f.get('#p', []) and 25050 in f.get('kinds', [])
                                   for subs in relay.subs.values() for fs in subs.values() for f in fs)
                    for _ in range(150):
                        if all(armed(p) for p in ps): break
                        await asyncio.sleep(.2)
                    else:
                        raise AssertionError('call signalling never subscribed: ' + str([p.name for p in ps if not armed(p)]))
                    await check(relay, *ps)
                    for p in ps:
                        assert not await p.js('__errors'), (p.name, await p.js('__errors'))
            finally:
                http.shutdown(); http.server_close()
                server.close(); await server.wait_closed()
        return lambda: asyncio.run(run())
    return wrap


ALICE, BOB, CAROL = (1, 'alice'), (2, 'bob'), (3, 'carol')


# ---------------------------------------------------------------------------------------------------
# 1:1 voice
# ---------------------------------------------------------------------------------------------------
@scenario(ALICE, BOB)
async def _voice_call_rings_connects_and_ends_on_both_sides(relay, alice, bob):
    await alice.call(bob)
    await alice.until("!!document.querySelector('#call-overlay')", what='her own call screen')
    # Bob's screen rings, with Answer and Decline — the only two things he can do.
    await bob.until("(document.querySelector('#call-overlay')||{}).className?.includes('ring')", what='the incoming call')
    ov = await bob.overlay()
    assert ov['buttons'] == ['Answer', 'Decline'], ov
    assert 'incoming call' in ov['status'], ov
    # Exactly ONE frame may wake a phone: the invite (test_call_ring_filter holds the server to it).
    invites = [e for e in relay.frames() if ['t', 'invite'] in e['tags']]
    assert len(invites) == 1 and ['p', bob.pubkey] in invites[0]['tags'], invites
    # The body is encrypted: no SDP, no "invite" word in what the relay saw.
    assert all('sdp' not in e['content'] and 'v=0' not in e['content'] for e in relay.frames())

    await bob.press('call-accept')
    for p in (alice, bob):
        await p.until("(document.querySelector('#call-status')||{}).textContent==='connected'", what='"connected"')
        await p.receives('audio', 'the other person\'s voice')
    # Audio-first: neither side is sending video, and both see the voice layout with the in-call controls.
    assert await alice.media_in('video') == 0
    ov = await alice.overlay()
    assert ' aud' in ov['cls'] and ' on' in ov['cls'], ov
    assert {'Mute', 'Start video', 'End'} <= set(ov['buttons']), ov

    await alice.press('call-hang')
    await alice.until("!document.querySelector('#call-overlay')", what='her call screen to close')
    await bob.until("!document.querySelector('#call-overlay')", what='HIS call screen to close when she hung up')
    for p in (alice, bob):
        assert await p.live_connections() == 0, f'{p.name} still holds a connection after the call ended'
        # The microphone is released, not just the screen closed (a lit mic indicator after a call).
        assert await p.js("__pcs.every(pc=>pc.getSenders().every(s=>!s.track||s.track.readyState==='ended'))")
    assert await bob.missed() == [], 'an ANSWERED call must never be logged as missed'


def test_voice_call_rings_connects_and_ends_on_both_sides():
    _voice_call_rings_connects_and_ends_on_both_sides()


@scenario(ALICE, BOB)
async def _declining_ends_the_call_for_the_caller(relay, alice, bob):
    await alice.call(bob)
    await bob.until("!!document.getElementById('call-decline')", what='the incoming call')
    await bob.press('call-decline')
    await bob.until("!document.querySelector('#call-overlay')", what='his screen to stop ringing')
    await alice.until("!document.querySelector('#call-overlay')", what='her call to end when he declined')
    assert await alice.live_connections() == 0
    assert await bob.missed() == [], 'a call he DECLINED is not a call he missed'


def test_declining_ends_the_call_for_the_caller():
    _declining_ends_the_call_for_the_caller()


@scenario(ALICE, BOB)
async def _a_call_the_caller_abandons_is_logged_as_missed(relay, alice, bob):
    await alice.call(bob)
    await bob.until("!!document.getElementById('call-accept')", what='the incoming call')
    await alice.press('call-hang')
    await bob.until("!document.querySelector('#call-overlay')", what='his phone to stop ringing when she gave up')
    assert await bob.missed() == [alice.pubkey]
    # And the Calls screen tells him so.
    await bob.js("__PC.switchView('calls')")
    await bob.until("!!document.querySelector('.call-missed .call-contact[data-pk]')", what='the Missed list')
    assert await bob.js("document.querySelector('.call-missed .call-contact').dataset.pk") == alice.pubkey
    assert await alice.missed() == [], 'the CALLER did not miss anything'


def test_a_call_the_caller_abandons_is_logged_as_missed():
    _a_call_the_caller_abandons_is_logged_as_missed()


# ---------------------------------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------------------------------
@scenario(ALICE, BOB)
async def _video_can_be_turned_on_and_off_mid_call(relay, alice, bob):
    await alice.call(bob)
    await bob.press('call-accept')
    for p in (alice, bob):
        await p.until("(document.querySelector('#call-status')||{}).textContent==='connected'", what='"connected"')
    # Audio-first call -> Start video: renegotiates the SAME connection, Bob starts receiving pictures.
    await alice.press('call-cam')
    await bob.receives('video', 'her camera')
    await bob.until("(document.querySelector('#call-overlay')||{}).className?.includes(' vid')", what='the video layout')
    await bob.until("(()=>{const v=document.getElementById('call-remote');return v&&v.style.display!=='none'&&v.videoWidth>0})()",
                    what='her picture to be showing (decoded frames, not just a track)')
    # Still one connection each — video was added by renegotiation, not by a second call.
    assert await alice.live_connections() == 1 and await bob.live_connections() == 1
    ov = await alice.overlay()
    assert 'Stop video' in ov['buttons'], ov
    # Her own preview is on her screen.
    await alice.until("(()=>{const v=document.getElementById('call-local');return v&&v.style.display!=='none'})()", what='her self-view')

    # Stop video: her camera is released and the call carries on as voice.
    await alice.press('call-cam')
    await alice.until("(()=>{const o=document.querySelector('#call-overlay');return o&&o.className.includes(' aud')})()", what='her voice layout')
    assert await alice.js("__pcs.filter(p=>p.connectionState==='connected').every(p=>p.getSenders().every(s=>!s.track||s.track.kind!=='video'))"), \
        'her camera is still being sent after Stop video'
    await bob.receives('audio', 'her voice after video stopped')
    assert (await alice.overlay())['status'] == 'connected'


def test_video_can_be_turned_on_and_off_mid_call():
    _video_can_be_turned_on_and_off_mid_call()


@scenario(ALICE, BOB)
async def _mute_silences_the_microphone_not_the_call(relay, alice, bob):
    await alice.call(bob)
    await bob.press('call-accept')
    await alice.until("(document.querySelector('#call-status')||{}).textContent==='connected'", what='"connected"')
    await alice.press('call-mute')
    await alice.until("(()=>{const o=document.querySelector('#call-overlay');return o&&[...o.querySelectorAll('.call-act span')].some(s=>s.textContent==='Unmute')})()",
                      what='the Unmute button')
    assert await alice.js("__pcs.filter(p=>p.connectionState==='connected').every(p=>p.getSenders().filter(s=>s.track&&s.track.kind==='audio').every(s=>!s.track.enabled))")
    assert await alice.live_connections() == 1, 'mute ended the call'
    await alice.press('call-mute')
    assert await alice.js("__pcs.filter(p=>p.connectionState==='connected').every(p=>p.getSenders().filter(s=>s.track&&s.track.kind==='audio').every(s=>s.track.enabled))")


def test_mute_silences_the_microphone_not_the_call():
    _mute_silences_the_microphone_not_the_call()


# ---------------------------------------------------------------------------------------------------
# Races: busy, glare, another of my devices
# ---------------------------------------------------------------------------------------------------
@scenario(ALICE, BOB, CAROL)
async def _calling_someone_already_on_a_call_gets_busy(relay, alice, bob, carol):
    await alice.call(bob)
    await bob.press('call-accept')
    await bob.until("(document.querySelector('#call-status')||{}).textContent==='connected'", what='"connected"')
    await carol.call(bob)
    await carol.until("!document.querySelector('#call-overlay')", what='Carol to be turned away (busy)')
    # Bob's call with Alice is untouched — no ringing over it, still connected, still talking.
    ov = await bob.overlay()
    assert ov['status'] == 'connected' and 'ring' not in ov['cls'], ov
    await bob.receives('audio', 'Alice, still')


def test_calling_someone_already_on_a_call_gets_busy():
    _calling_someone_already_on_a_call_gets_busy()


@scenario(ALICE, BOB)
async def _two_people_calling_each_other_at_once_end_up_on_one_call(relay, alice, bob):
    await alice.js("__PC.switchView('calls')")
    await bob.js("__PC.switchView('calls')")
    await alice.until("!!document.querySelector('#call-npub')")
    await bob.until("!!document.querySelector('#call-npub')")
    # Both press Call in the same instant.
    await asyncio.gather(
        alice.js(f"document.querySelector('#call-npub').value={json.dumps(bob.npub)};document.querySelector('#call-start-btn').click()"),
        bob.js(f"document.querySelector('#call-npub').value={json.dumps(alice.npub)};document.querySelector('#call-start-btn').click()"))
    # The higher pubkey yields and rings; whoever rings answers, and they converge on ONE call.
    lower, higher = sorted((alice, bob), key=lambda p: p.pubkey)
    await higher.until("!!document.getElementById('call-accept')", what='the higher key to yield and ring')
    await higher.press('call-accept')
    for p in (alice, bob):
        await p.until("(document.querySelector('#call-status')||{}).textContent==='connected'", what='"connected"')
        await p.receives('audio')
        assert await p.live_connections() == 1, f'{p.name} has two calls up after glare'


def test_two_people_calling_each_other_at_once_end_up_on_one_call():
    _two_people_calling_each_other_at_once_end_up_on_one_call()


@scenario(ALICE, BOB, (2, 'bob-phone'))
async def _answering_on_one_device_stops_the_others_ringing(relay, alice, bob, bob_phone):
    await alice.call(bob)
    await bob.until("!!document.getElementById('call-accept')", what='his desk to ring')
    await bob_phone.until("!!document.getElementById('call-accept')", what='his phone to ring')
    await bob.press('call-accept')
    await bob_phone.until("!document.querySelector('#call-overlay')", what='his phone to stop ringing once the desk answered')
    await bob.until("(document.querySelector('#call-status')||{}).textContent==='connected'", what='"connected"')
    await alice.receives('audio')
    assert await bob_phone.missed() == [], 'a call answered on another device was logged as missed'
    assert await bob_phone.live_connections() == 0


def test_answering_on_one_device_stops_the_others_ringing():
    _answering_on_one_device_stops_the_others_ringing()


# ---------------------------------------------------------------------------------------------------
# Group calls (mesh)
# ---------------------------------------------------------------------------------------------------
async def _start_group(host, *guests):
    await host.js("__PC.switchView('calls')")
    await host.until("!!document.querySelector('#call-npub')")
    await host.js(f"__PC.startGroupCall({json.dumps([g.pubkey for g in guests])}, false)")
    await host.until("!!document.querySelector('#room-overlay')", what='the group call screen')


async def _connected_to(p, n, timeout=30):
    await p.until(f"__pcs.filter(x=>x.connectionState==='connected').length==={n}", timeout=timeout,
                  what=f'{n} live connection(s) in the group call')


@scenario(ALICE, BOB, CAROL)
async def _group_call_connects_everyone_to_everyone(relay, alice, bob, carol):
    await _start_group(alice, bob, carol)
    for g in (bob, carol):
        await g.until("!!document.getElementById('room-accept')", what='the group invite')
        assert '3 people' in await g.js("document.querySelector('#room-overlay .room-hd').textContent")
    await bob.press('room-accept')
    await carol.press('room-accept')
    # Mesh: each of the three holds a live connection to BOTH others, and hears them.
    for p in (alice, bob, carol):
        await _connected_to(p, 2)
        await p.receives('audio')
        # One tile per person, and every remote tile is bound to a live stream.
        assert await p.js("document.querySelectorAll('#room-overlay .call-tile').length") == 3
        await p.until("[...document.querySelectorAll('#room-overlay video.room-remote')].every(v=>v.srcObject&&v.srcObject.getAudioTracks().length)",
                      what='both remote tiles to carry a stream')
    # _connected_to(p, 2) is EXACTLY two: a pair that both offered would show up here as 3 or 4.


def test_group_call_connects_everyone_to_everyone():
    _group_call_connects_everyone_to_everyone()


@scenario(ALICE, BOB, CAROL)
async def _leaving_a_group_call_keeps_the_others_talking(relay, alice, bob, carol):
    await _start_group(alice, bob, carol)
    for g in (bob, carol):
        await g.press('room-accept')
    for p in (alice, bob, carol):
        await _connected_to(p, 2)
    await carol.press('room-hang')
    await carol.until("!document.querySelector('#room-overlay')", what='her group call screen to close')
    assert await carol.live_connections() == 0
    for p in (alice, bob):
        # Promptly: her connections close (the remote sees `closed` at once) and she sends rbye. If both
        # were lost the others would only notice when ICE gave up on her, ~30s later.
        await _connected_to(p, 1, timeout=6)
        await p.receives('audio', 'the one person still on the call')
        assert await p.js("!!document.querySelector('#room-overlay')"), f'{p.name} lost the group call when Carol left'
    # The HOST leaving does not end it for the rest either (a mesh has no host).
    await alice.press('room-hang')
    await _connected_to(bob, 0)
    await alice.until("!document.querySelector('#room-overlay')")


def test_leaving_a_group_call_keeps_the_others_talking():
    _leaving_a_group_call_keeps_the_others_talking()


@scenario(ALICE, BOB, CAROL)
async def _declining_a_group_call_does_not_stop_it(relay, alice, bob, carol):
    await _start_group(alice, bob, carol)
    await bob.until("!!document.getElementById('room-decline')", what='the group invite')
    await bob.press('room-decline')
    await bob.until("!document.querySelector('#room-overlay')", what='his screen to stop ringing')
    await carol.press('room-accept')
    await _connected_to(alice, 1)
    await _connected_to(carol, 1)
    await carol.receives('audio')
    assert await bob.live_connections() == 0


def test_declining_a_group_call_does_not_stop_it():
    _declining_a_group_call_does_not_stop_it()


@scenario(ALICE, BOB, CAROL)
async def _a_group_video_call_shows_every_face(relay, alice, bob, carol):
    await alice.js("__PC.switchView('calls')")
    await alice.js(f"__PC.startGroupCall({json.dumps([bob.pubkey, carol.pubkey])}, true)")
    for g in (bob, carol):
        await g.press('room-accept')
    for p in (alice, bob, carol):
        await _connected_to(p, 2)
        await p.receives('video')
        await p.until("[...document.querySelectorAll('#room-overlay video.room-remote')].length===2 && "
                      "[...document.querySelectorAll('#room-overlay video.room-remote')].every(v=>v.style.display!=='none'&&v.videoWidth>0)",
                      what='both other faces on screen')


def test_a_group_video_call_shows_every_face():
    _a_group_video_call_shows_every_face()
