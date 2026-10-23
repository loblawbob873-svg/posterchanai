"""LIVE STREAMS, END TO END: the list, a stream that plays, its chat between two people, an ended stream, and every
way the stream's source or the relay can fail -- each judged by what the PERSON sees.

The other stream suites press the buttons on one page with a stubbed relay (test_streams_player_buttons,
test_stream_page_buttons) or read the source (test_stream_session_and_admin_claim). Here each person is a fresh
Chrome profile running the SHIPPED client against this node's REAL relay (tests/client/realnode_harness.py:
RelayServer over PosterChanDB, NIP-42, the WoT gate). The stream itself is served over real HLS (ffmpeg-made
segments played by the vendored hls.js) from a loopback server that can also answer 404, refuse the connection,
or accept and never answer.

The 30311 announcement is written into the relay the way another NIP-53 client (zap.stream, Amethyst) publishes
one -- Go Live itself needs MediaMTX, which is not here. Everything that crosses between the two people (the
kind-1311 chat) is made by the shipped client's own Send button.

  watch + chat   Alice and Bob, each on a fresh device: the list shows the live stream first and the ended one
                 badged "ended"; the live one PLAYS (decoded frames); Alice's line reaches Bob's open stream with
                 no reload, his reply reaches hers, and the relay holds both on the stream's `a` coordinate.
  ended          an ended stream whose `streaming` tag still names the dead live feed (every stream Go Live ever
                 ended looks like this) says it ended, and a just-ended one says the recording is being saved --
                 never "could not play this stream" from trying the dead feed, and never a black box.
  broken source  a live stream whose HLS answers 404 / refuses the connection / never answers shows an explicit
                 message within seconds; the recordings endpoint answering 503 or never answering is "could not
                 check", not "no recording".
  relays         a fresh device whose relay never answers does not state "No live streams right now" as a fact
                 and does not spin for ever; Retry finds the stream once the relay answers; a SLOW relay's
                 stream appears without the empty-list claim ever being made first.

Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/streams.js=<older copy>" serves another copy of the module.
"""
import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import websockets

from tests.client import realnode_harness as R
from tests.client import wallpaper_harness as H

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
# A committed stream, not one made by ffmpeg at test time (the desktop CI runner has no ffmpeg; a skip there fails the
# build). Made once with: ffmpeg -f lavfi -i testsrc=size=160x120:rate=10 -t 6 -c:v libx264 -pix_fmt yuv420p -g 10
#   -f hls -hls_time 1 -hls_list_size 0 tests/fixtures/hls_stream/index.m3u8
HLS_FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'fixtures', 'hls_stream')
ALICE, BOB, CAROL = 31, 32, 33        # account keys not used by other suites
CFG = {'wot_enabled': True}


# ------------------------------------------------------------------------------------------------ the stream source
class HlsServer:
    """Loopback HLS: /ok/ is a real ffmpeg-made stream, /missing/ answers 404, /hang/ accepts and never answers."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='pc-hls-')
        for name in os.listdir(HLS_FIXTURE):
            shutil.copyfile(os.path.join(HLS_FIXTURE, name), os.path.join(self.dir, name))
        self.release = threading.Event()
        self.hits = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                p = self.path.split('?')[0]
                outer.hits.append(p)
                if p.startswith('/hang/'):
                    outer.release.wait(120)          # a server that took the request and never answers
                    return
                name = p.rsplit('/', 1)[-1]
                f = os.path.join(outer.dir, name)
                if not p.startswith('/ok/') or not os.path.isfile(f):
                    self.send_response(404)
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Content-Length', '9')
                    self.end_headers()
                    self.wfile.write(b'not found')
                    return
                data = open(f, 'rb').read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.apple.mpegurl' if name.endswith('.m3u8') else 'video/mp2t')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.http.server_address[1]

    def close(self):
        self.release.set()
        self.http.shutdown()
        self.http.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def stream(author, d, title, status, url, ago=60, extra=()):
    now = int(time.time())
    tags = [['d', d], ['title', title], ['status', status], ['streaming', url], ['starts', str(now - ago - 600)]]
    if status == 'ended':
        tags.append(['ends', str(now - ago)])
    return R.sign(author, 30311, '', tags + list(extra), created_at=now - ago)


def addr(ev):
    return '30311:%s:%s' % (ev['pubkey'], next(t[1] for t in ev['tags'] if t[0] == 'd'))


# ------------------------------------------------------------------------------------------------ what a person does
async def open_streams(d):
    await d.b.js("__PC.switchView('streams');true")
    await R.wait(d.b, "!!document.querySelector('.streams-top')", 30, 'the Streams screen')


async def open_card(d, title):
    card = "[...document.querySelectorAll('.stream-card')].find(c=>c.querySelector('.stream-title').textContent===%s)" % json.dumps(title)
    await R.wait(d.b, '!!' + card, 30, 'the %r card' % title)
    await d.b.js(card + '.click();true')
    await R.wait(d.b, "!!document.querySelector('.stream-view') && document.querySelector('.av-title').textContent.includes(%s)"
                 % json.dumps(title), 20, 'the %r stream page' % title)


NOTE = "((document.querySelector('#st-note')||{}).textContent||'')"
VIDEO_SHOWN = "(()=>{const v=document.querySelector('#st-video');return !!v && !v.hidden && v.getClientRects().length>0})()"
CHAT = "[...document.querySelectorAll('#st-chat-msgs .scm-txt')].map(x=>x.textContent.trim())"


async def say(d, text):
    await d.b.js("(()=>{const i=document.querySelector('#st-chat-inp');i.value=%s;document.querySelector('#st-chat-send').click();"
                 "return true})()" % json.dumps(text))


async def chat_shows(d, lines, what):
    await R.wait(d.b, 'JSON.stringify(%s)===%s' % (CHAT, json.dumps(json.dumps(lines, separators=(',', ':')))), 30, what,
                 state="({chat:%s,toasts:(window.__toastLog||[]).slice(-5),errors:(window.__errors||[]).slice(-5)})" % CHAT)


def run(scenario, **node):
    async def main():
        hls = HlsServer()
        try:
            with R.Node(cfg=CFG, members=[R.pubkey(ALICE), R.pubkey(BOB), R.pubkey(CAROL)], **node) as n:
                await scenario(n, hls)
        finally:
            hls.close()
    asyncio.run(main())


def clean(errors):
    return [e for e in errors if 'ResizeObserver' not in e]


# ------------------------------------------------------------------------------------------------ watch + chat
@CHROME
def test_two_people_watch_a_live_stream_and_chat_on_it():
    async def scenario(n, hls):
        live = stream(CAROL, 'carol-live', 'Carol live', 'live', hls.base + '/ok/index.m3u8', ago=30)
        # Ended by Go Live: `streaming` still names this app's HLS proxy path for the stopped feed.
        ended = stream(CAROL, 'carol-old', 'Carol last week', 'ended', hls.base + '/api/streams/hls/carol/index.m3u8',
                       ago=7 * 86400)
        n.home.add([live, ended])
        R.NODE['api']['/api/streams/vods/by-token/carol-old'] = {'vods': []}
        async with R.Device(n, ALICE) as alice, R.Device(n, BOB) as bob:
            for d in (alice, bob):
                await open_streams(d)
                await R.wait(d.b, "document.querySelectorAll('.stream-card').length===2", 30, 'both streams listed')
                # Live first, with its badge; the ended one after it, badged "ended" and NOT live.
                cards = await d.b.js("[...document.querySelectorAll('.stream-card')].map(c=>[c.querySelector('.stream-title').textContent,"
                                     "!!c.querySelector('.live-badge'),!!c.querySelector('.ended-badge')])")
                assert cards == [['Carol live', True, False], ['Carol last week', False, True]], cards
                await open_card(d, 'Carol live')
            # The stream PLAYS: decoded frames from the real HLS, not a black box.
            await R.wait(alice.b, "(()=>{const v=document.querySelector('#st-video');return v&&v.readyState>=2&&v.videoWidth===160})()",
                         30, 'the live picture decoded on Alice\'s screen',
                         state="({note:%s,hits:%s})" % (NOTE, json.dumps(hls.hits[-5:])))
            await say(alice, 'hello from alice')
            await chat_shows(alice, ['hello from alice'], "Alice's own line in her chat")
            await chat_shows(bob, ['hello from alice'], "Alice's line on Bob's open stream, no reload")
            await asyncio.sleep(1.1)                # chat is ordered by created_at, in seconds
            await say(bob, 'hi alice, great stream')
            await chat_shows(bob, ['hello from alice', 'hi alice, great stream'], "Bob's reply in his chat")
            await chat_shows(alice, ['hello from alice', 'hi alice, great stream'], "Bob's reply on Alice's screen")
            held = n.home.query({'kinds': [1311], '#a': [addr(live)]})
            assert sorted(e['pubkey'] for e in held) == sorted([R.pubkey(ALICE), R.pubkey(BOB)]), held
            # The ended stream: said to be over, and no dead player.
            await bob.b.js("document.querySelector('#st-back').click();true")
            await open_card(bob, 'Carol last week')
            await R.wait(bob.b, NOTE + "==='This stream ended — no recording available.'", 20,
                         'the ended stream says it ended with no recording', state="({note:%s,video:%s})" % (NOTE, VIDEO_SHOWN))
            title = await bob.b.js("document.querySelector('.stream-view').innerText")
            assert '● LIVE' not in title and '· ended' in title, title
            assert not await bob.b.js(VIDEO_SHOWN), 'an ended stream with nothing to play still shows a black player'
            assert '/api/streams/hls/carol/index.m3u8' not in hls.hits, 'the ended stream tried to play its dead live feed'
            for d in (alice, bob):
                assert not clean(await d.b.js('__errors')), await d.b.js('__errors')
    run(scenario)


# ------------------------------------------------------------------------------------------------ ended elsewhere
SAID_CANT_PLAY = r'''
window.__cantPlay=[];
new MutationObserver(()=>{const n=document.querySelector('#st-note');
  if(n&&/could not play|isn\u2019t answering/i.test(n.textContent))window.__cantPlay.push(n.textContent);})
  .observe(document,{childList:true,subtree:true,characterData:true});
'''


@CHROME
def test_an_ended_stream_whose_replay_is_at_its_streaming_url_plays_it():
    """Another NIP-53 host serves an ended stream's replay at the `streaming` URL (no `recording` tag, no recording on
    this node): it PLAYS. Go Live's own ended feed (this node's HLS proxy path) is never tried -- its replay is the
    recording this node keeps -- and another host's dead feed ends in the plain "ended" message, never in "could not
    play" and never a black player."""
    async def scenario(n, hls):
        replay = stream(CAROL, 'elsewhere-replay', 'Replay elsewhere', 'ended', hls.base + '/ok/index.m3u8', ago=86400)
        dead = stream(CAROL, 'elsewhere-dead', 'Dead elsewhere', 'ended', hls.base + '/missing/gone.m3u8', ago=86400)
        ours = stream(CAROL, 'ours-ended', 'Ours ended', 'ended', hls.base + '/api/streams/hls/ours/index.m3u8', ago=86400)
        n.home.add([replay, dead, ours])
        for d in ('elsewhere-replay', 'elsewhere-dead', 'ours-ended'):
            R.NODE['api']['/api/streams/vods/by-token/' + d] = {'vods': []}
        async with R.Device(n, BOB) as bob:
            await bob.b.js(SAID_CANT_PLAY)
            await open_streams(bob)
            await open_card(bob, 'Replay elsewhere')
            await R.wait(bob.b, "(()=>{const v=document.querySelector('#st-video');return v&&v.readyState>=2&&v.videoWidth===160})()",
                         30, 'the replay decoded on screen', state="({note:%s,hits:%s})" % (NOTE, json.dumps(hls.hits[-5:])))
            assert await bob.b.js(VIDEO_SHOWN), 'the replay plays in a hidden player'
            assert 'no recording' not in await bob.b.js(NOTE)
            await bob.b.js("document.querySelector('#st-back').click();true")
            for title in ('Dead elsewhere', 'Ours ended'):
                await open_card(bob, title)
                await R.wait(bob.b, NOTE + "==='This stream ended — no recording available.'", 25,
                             '%r says it ended with no recording' % title, state="({note:%s,video:%s})" % (NOTE, VIDEO_SHOWN))
                assert not await bob.b.js(VIDEO_SHOWN), '%r: a black player under the message' % title
                await bob.b.js("document.querySelector('#st-back').click();true")
                await R.wait(bob.b, "!!document.querySelector('.stream-grid')", 20, 'back on the list')
            assert not await bob.b.js('window.__cantPlay'), await bob.b.js('window.__cantPlay')
            assert '/api/streams/hls/ours/index.m3u8' not in hls.hits, "Go Live's own ended feed was tried"
            assert not clean(await bob.b.js('__errors')), await bob.b.js('__errors')
    run(scenario)


# ------------------------------------------------------------------------------------------------ broken sources
@CHROME
def test_a_stream_or_recording_that_cannot_be_reached_says_so():
    async def scenario(n, hls):
        dead = 'http://127.0.0.1:%d/live/index.m3u8' % closed_port()
        sources = {'Missing feed': hls.base + '/missing/index.m3u8', 'Refused feed': dead,
                   'Silent feed': hls.base + '/hang/index.m3u8'}
        evs = [stream(CAROL, 'k%d' % i, t, 'live', u, ago=20 + i) for i, (t, u) in enumerate(sources.items())]
        # Ended streams whose `streaming` still names the stopped feed, as Go Live leaves them: one ended a minute
        # ago (its recording is still being made), one whose recordings endpoint is down, one that never answers.
        evs.append(stream(CAROL, 'just-ended', 'Just ended', 'ended', hls.base + '/missing/x.m3u8', ago=60))
        evs.append(stream(CAROL, 'vod-down', 'Vod down', 'ended', hls.base + '/missing/y.m3u8', ago=86400))
        evs.append(stream(CAROL, 'vod-hang', 'Vod hang', 'ended', hls.base + '/missing/z.m3u8', ago=86400))
        n.home.add(evs)
        R.NODE['api']['/api/streams/vods/by-token/just-ended'] = {'vods': []}
        gate = threading.Event()

        def vods(handler, method, path, body):
            p = path.split('?')[0]
            if p == '/api/streams/vods/by-token/vod-down':
                handler._json({'detail': 'relay unavailable'}, 503)
                return True
            if p == '/api/streams/vods/by-token/vod-hang':
                gate.wait(60)
                return True
            return False
        H.ROUTES.append(vods)
        try:
            async with R.Device(n, BOB) as bob:
                await open_streams(bob)
                for title in sources:
                    await open_card(bob, title)
                    t0 = time.monotonic()
                    await R.wait(bob.b, "/could not|isn’t answering|couldn’t/i.test(%s)" % NOTE, 25,
                                 '%r: an explicit message instead of a black player' % title,
                                 state="({note:%s,ready:(document.querySelector('#st-video')||{}).readyState})" % NOTE)
                    print(title, 'said so after %.1fs:' % (time.monotonic() - t0), await bob.b.js(NOTE))
                    await bob.b.js("document.querySelector('#st-back').click();true")
                    await R.wait(bob.b, "!!document.querySelector('.stream-grid')", 20, 'back on the list')
                await open_card(bob, 'Just ended')
                await R.wait(bob.b, NOTE + ".startsWith('Saving your recording')", 20, 'a just-ended stream: the recording is coming',
                             state="({note:%s})" % NOTE)
                assert not await bob.b.js(VIDEO_SHOWN)
                await bob.b.js("document.querySelector('#st-back').click();true")
                for title in ('Vod down', 'Vod hang'):
                    await open_card(bob, title)
                    await R.wait(bob.b, "/couldn’t check/i.test(%s)" % NOTE, 25,
                                 '%r: "could not check for a recording", never "no recording available"' % title,
                                 state="({note:%s})" % NOTE)
                    assert 'no recording available' not in await bob.b.js(NOTE)
                    assert not await bob.b.js(VIDEO_SHOWN)
                    await bob.b.js("document.querySelector('#st-back').click();true")
                    await R.wait(bob.b, "!!document.querySelector('.stream-grid')", 20, 'back on the list')
                assert not clean(await bob.b.js('__errors')), await bob.b.js('__errors')
        finally:
            gate.set()
            H.ROUTES.remove(vods)
    run(scenario)


# ------------------------------------------------------------------------------------------------ relays
class SlowRelay:
    """A relay that holds `events` and answers a REQ after `delay` seconds -- or never, while `silent`."""

    def __init__(self, loop, events, delay=0.0, silent=False):
        self.events, self.delay, self.silent = list(events), delay, silent
        self.reqs = 0

        async def handler(ws):
            try:
                async for raw in ws:
                    m = json.loads(raw)
                    if m[0] != 'REQ':
                        continue
                    self.reqs += 1
                    if self.silent:
                        continue
                    asyncio.ensure_future(self._answer(ws, m[1], m[2:]))
            except websockets.ConnectionClosed:
                pass

        self.server = loop.serve(handler, None)
        self.url = 'ws://127.0.0.1:%d' % self.server.sockets[0].getsockname()[1]

    async def _answer(self, ws, sid, filters):
        await asyncio.sleep(self.delay)
        try:
            for e in self.events:
                if any(R._match(f, e) for f in filters):
                    await ws.send(json.dumps(['EVENT', sid, e]))
            await ws.send(json.dumps(['EOSE', sid]))
        except websockets.ConnectionClosed:
            pass


SAID_NONE = r'''
window.__saidNone=0;
new MutationObserver(()=>{const f=document.querySelector('#feed .empty');
  if(f&&/No live streams right now/.test(f.textContent))window.__saidNone++;})
  .observe(document,{childList:true,subtree:true,characterData:true});
'''


@CHROME
def test_a_relay_that_never_answers_or_answers_slowly():
    async def scenario(n, hls):
        live = stream(CAROL, 'r-live', 'Relay stream', 'live', hls.base + '/ok/index.m3u8', ago=10)
        relay = SlowRelay(n.loop, [live], silent=True)
        R.NODE['config'] = {'relay_url': relay.url, 'relays': [relay.url], 'default_relays': [relay.url]}
        async with R.Device(n, BOB, pre_init=SAID_NONE) as bob:
            await open_streams(bob)
            # Within the read's deadline the screen says the relays did not answer -- not "no streams", no spinner.
            await R.wait(bob.b, "/didn’t answer/.test((document.querySelector('#feed .empty')||{}).textContent||'')", 15,
                         'the list says the relay did not answer',
                         state="({feed:document.querySelector('#feed').innerText.slice(0,300),reqs:%d})" % relay.reqs)
            assert not await bob.b.js("!!document.querySelector('#feed .spinner')"), 'still spinning after the deadline'
            assert await bob.b.js('__saidNone') == 0, '"No live streams right now" was stated while the relay never answered'
            # The relay comes back: Retry finds the stream.
            relay.silent = False
            await bob.b.js("document.querySelector('#streams-retry').click();true")
            await R.wait(bob.b, "[...document.querySelectorAll('.stream-card .stream-title')].some(x=>x.textContent==='Relay stream')",
                         15, 'Retry lists the stream once the relay answers')
            assert not clean(await bob.b.js('__errors')), await bob.b.js('__errors')
        # A SLOW relay (3 s to answer): the stream appears, and the empty-list claim is never made while waiting.
        slow = SlowRelay(n.loop, [live], delay=3.0)
        R.NODE['config'] = {'relay_url': slow.url, 'relays': [slow.url], 'default_relays': [slow.url]}
        async with R.Device(n, ALICE, pre_init=SAID_NONE) as alice:
            await open_streams(alice)
            await R.wait(alice.b, "[...document.querySelectorAll('.stream-card .stream-title')].some(x=>x.textContent==='Relay stream')",
                         20, 'the slow relay\'s stream is listed')
            assert await alice.b.js('__saidNone') == 0, '"No live streams right now" was shown before the slow relay answered'
    run(scenario)
