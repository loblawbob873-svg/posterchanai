"""GO LIVE AGAINST THE REAL MediaMTX POSTERCHAN SHIPS -- not a relay event standing in for a stream.

Every other stream test writes the NIP-53 announcement into the relay by hand and serves a committed HLS fixture,
because "Go Live needs MediaMTX". MediaMTX is not an outside service: scripts/install/stream.sh downloads a PINNED
release and the app supervises it. So here:

  MediaMTX  that pinned release (tests/client/mediamtx_server.py: fetched once, sha256-verified, cached), started BY
            app/services/stream_service.py's own `_spawn` with the config its own `_write_config` renders -- free
            ports, its streamserver/ dir and the recordings in a temp dir, the bitrate clamp ON (the node default; CPU
            encoder so a test never takes the GPU's media engine), recording ON. Its auth and unpublish hooks call the
            instance below, which hands them to the REAL app/routers/streams.py. Killed and removed at the end, with
            an assertion that nothing of it (or its clamp ffmpeg) is left running. Never the node's media service.
  the node  tests/client/realnode_harness.py (the shipped relay over PosterChanDB, the instance, real Chrome) with
            app/routers/streams.py and app/routers/blossom.py mounted IN-PROCESS behind the instance, the accounts,
            keys and settings in the app's own DocTables (in memory), and the REAL stream-end reaper running
            (stream_end_service + stream_vod_service, sweep intervals shortened).
  Chrome    with a fake camera and microphone (--use-fake-device-for-media-stream --use-fake-ui-for-media-stream),
            the way a person's browser grants them.

Flows (each its own test):
  1  Go Live from the SHIPPED client: Streams -> "Go Live" -> "This webcam" -> "Announce and Stream" publishes
     WebRTC over WHIP (the app's /api/streams/whip proxy -> MediaMTX, whose auth hook the app answers); the kind-30311
     is published by the client's own code; a second account sees it in the list, opens it and the picture DECODES
     (frames advance, not a flat image) through the app's HLS proxy; chat crosses both ways.
  2  Ending it: "Stop streaming" -> the announcement flips to ended (with `ends`), the streamer's own page stops saying
     "You're broadcasting" at once, the viewer's OPEN page turns into the ended stream (never "could not play" under a
     LIVE badge) and the list badges it ended; the streamer closes the app; the recording MediaMTX made is claimed,
     compressed and stored on the drive by the real reaper (held until the viewer has seen "saving", as when it is
     slow), and the viewer plays it as the replay -- which keeps playing (no stale re-check re-renders under it).
  3  Auth against the real MediaMTX: an RTMP publish (ffmpeg) with a forged key, with ANOTHER user's key on your
     path, and a WHIP offer straight to MediaMTX with a forged key are all refused BY MediaMTX through the hook;
     the owner's own key is accepted (the control). The hook routes themselves: tests/test_stream_auth_hook.py.
  4  Failure: MediaMTX down -> the person is told plainly and NO live announcement is published; MediaMTX refusing
     the publish (a revoked key) -> the same; a broadcaster whose tab dies mid-stream -> the server publishes the
     parked "ended" event, so no announcement is left claiming a stream that is not there.

WHERE THIS RUNS: the deploy gate's full pass (every tests/**/test_*.py) on a node. It is not a *_full_app.py and not
on the desktop CI's required list: that runner has no MediaMTX (and no network to fetch it), and here it SKIPS when the
pinned binary cannot be obtained, which the full pass allows. $PC_MEDIAMTX_BIN points it at a binary.

Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/streams.js=/tmp/old/streams.js".
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from tests.client import discover_flows_harness as F
from tests.client import mediamtx_server as M
from tests.client import realnode_harness as R

pytestmark = [pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required'),
              pytest.mark.skipif(not M.available(), reason='no pinned MediaMTX (%s)' % M.why()),
              pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required (the clamp and the recording)')]

FIRST_FILL = 150
SCENARIOS = {}
# F.run runs the most expensive first; the scenario that takes MediaMTX down must be LAST, so it is the cheapest.
COST = {'a_webcam_go_live_is_watched_chatted_ended_and_replayed': 90,
        'the_server_ends_a_stream_whose_tab_died': 60,
        'mediamtx_refuses_forged_and_borrowed_keys': 40,
        'an_obs_stream_that_reconnects_becomes_one_complete_vod': 50,
        'a_vod_whose_compression_fails_is_still_the_whole_broadcast': 45,
        'a_go_live_that_cannot_publish_says_so_and_announces_nothing': 1}
FAKE_CAM = ('--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
            '--autoplay-policy=no-user-gesture-required')


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# --------------------------------------------------------------------------------------------- the node
class Users(dict):
    """pubkey -> account id; reading one returns the account as the table holds it NOW (a toggle saved by a route is
    seen by the next request, as on a node)."""

    def __getitem__(self, pk):
        from app.services import user_table
        return user_table.get(None, dict.__getitem__(self, pk))


class Ctx:
    def __init__(self, n):
        from app.routers import blossom, streams
        from app.services import settings_store, stream_end_service, stream_vod_service
        from tests.doc_table_mem import mem_tables
        from tests.user_fixtures import seed_user
        self.n = n
        self.mp = mp = pytest.MonkeyPatch()
        self.mem = mem_tables(mp)
        self.users = Users()
        for i, fill in enumerate(range(FIRST_FILL, FIRST_FILL + 4 * len(SCENARIOS))):
            pk = R.pubkey(fill)
            seed_user(self.mem, 4000 + i, username='s%d' % fill, nostr_npub=pk, can_stream=True, stream_record=True)
            dict.__setitem__(self.users, pk, 4000 + i)
        self.blobs = os.path.join(n.tmp, 'blobs')
        for k, v in {'nostr_relay_port': str(n.home.port), 'blossom_enabled': 'true',
                     'blossom_storage_backend': 'local', 'blossom_storage_path': self.blobs}.items():
            mp.setitem(settings_store._CACHE, k, v)
        from app.services import blossom_service
        mp.setattr(blossom_service, '_DEFAULT_BLOB_DIR', self.blobs)      # never the checkout's data/blossom
        mp.setenv('VIDEO_HWACCEL', '0')          # the VOD's compression pass: CPU, never the node's GPU
        # The reaper's clocks, shortened (production: 30 s sweeps, a 20 s hook grace, 60 s of recording silence).
        mp.setattr(stream_end_service, '_SWEEP_INTERVAL', 2)
        mp.setattr(stream_end_service, '_HOOK_GRACE', 3)
        mp.setattr(stream_vod_service, '_END_QUIET_S', 4)
        mp.setattr(stream_vod_service, '_QUIET_S', 2)
        # A scenario may HOLD the recording worker (a recording that is still being made when a viewer arrives);
        # open unless a scenario clears it.
        import threading
        self.vod_open = threading.Event()
        self.vod_open.set()
        real_pp = stream_vod_service.process_pending

        async def process_pending():
            if self.vod_open.is_set():
                await real_pp()
        mp.setattr(stream_vod_service, 'process_pending', process_pending)
        # What the app's stream services and MediaMTX said (MediaMTX's log is piped into the app logger), kept so a
        # failure can say WHY -- a recording that never appears is otherwise just a timeout.
        import logging
        self.log = []

        class Keep(logging.Handler):
            def emit(h, rec):
                try:
                    self.log.append('%s %s' % (rec.name.rsplit('.', 1)[-1], rec.getMessage()))
                except Exception:
                    pass
        self._keep = Keep(logging.INFO)
        for name in ('app.services.stream_service', 'app.services.stream_vod_service',
                     'app.services.stream_end_service', 'app.routers.streams'):
            lg = logging.getLogger(name)
            lg.addHandler(self._keep)
            mp.setattr(lg, 'level', logging.INFO)
        self.mtx = M.MediaMTX(mp, n.port)
        t0 = time.monotonic()
        self.mtx.start()
        print('\n[mediamtx] private MediaMTX %s up in %.1fs (webrtc :%s, hls :%s)'
              % (M.version(), time.monotonic() - t0, self.mtx.port('webrtc'), self.mtx.port('hls')))
        self.api = F.Routers(mp, [(streams.router, ''), (blossom.router, '')], self.users,
                             paths=['/api/streams/', '/blossom/', '/api/calls/'],
                             forward=('host', 'range', 'content-range', 'accept-ranges', 'set-cookie', 'cookie'))
        # A node loads its tables at startup (table_migration.start_background_load); whole-table reads (a token's
        # recordings, the blob index) answer "loading" until then. Same here, once, before anyone asks.
        from app.services.doc_table import DocTable

        async def preload():
            for name in ('stream_vods', 'blossom_blobs', 'api_keys', 'api_key_index', 'user_settings', 'users'):
                await DocTable(name).aload()
        asyncio.run_coroutine_threadsafe(preload(), self.api.loop).result(30)
        self.api.loop.call_soon_threadsafe(stream_end_service.start_stream_end_reaper)

    def stop(self):
        from app.services import stream_end_service
        try:
            done = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), self.api.loop)
            done.result(5)
            self.api.loop.call_soon_threadsafe(stream_end_service.stop_stream_end_reaper)
            time.sleep(.3)
        except Exception:
            pass
        self.api.stop()
        import logging
        for name in ('app.services.stream_service', 'app.services.stream_vod_service',
                     'app.services.stream_end_service', 'app.routers.streams'):
            logging.getLogger(name).removeHandler(self._keep)
        try:
            self.mtx.stop()
        finally:
            self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    t0 = time.monotonic()
    check = F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx, lanes=1)
    print('\n[mediamtx] module ran in %.1fs' % (time.monotonic() - t0))
    return check


# --------------------------------------------------------------------------------------------- what a person does
NOTE = "((document.querySelector('#st-note')||{}).textContent||'')"
CHAT = "[...document.querySelectorAll('#st-chat-msgs .scm-txt')].map(x=>x.textContent.trim())"
# The picture is REALLY decoding: frames advance, and the frame is not one flat colour.
DECODED = r'''(()=>{const v=document.querySelector('#st-video');if(!v||v.hidden||v.readyState<2||!v.videoWidth)return false;
  const c=document.createElement('canvas');c.width=32;c.height=24;const x=c.getContext('2d');x.drawImage(v,0,0,32,24);
  const d=x.getImageData(0,0,32,24).data;let lo=255,hi=0;for(let i=0;i<d.length;i+=4){const l=d[i]+d[i+1]+d[i+2];
  if(l<lo)lo=l;if(l>hi)hi=l;}window.__frames=(window.__frames||[]);window.__frames.push(v.currentTime);
  return hi-lo>60;})()'''


async def open_streams(d):
    await d.b.js("__PC.switchView('streams');true")
    await F.wait(d, "!!document.querySelector('.streams-top')", 30, 'the Streams screen')


async def go_live(d, title, source='cam', before_press=None):
    """Streams -> Go Live -> pick the source -> type a title -> Announce and Stream, by the controls' ids."""
    await open_streams(d)
    await F.wait(d, "!!document.querySelector('#stream-golive')", 30, 'the Go Live button')
    await d.b.js("document.querySelector('#stream-golive').click();true")
    await F.wait(d, "!!document.querySelector('#gl-go')", 30, 'the Go Live sheet')
    await d.b.js("document.querySelector('input[name=gl-src][value=%s]').click();true" % source)
    if source == 'cam':
        await d.b.js("document.querySelector('input[name=gl-facing][value=user]').click();true")
    await d.b.js("(()=>{const t=document.querySelector('#gl-title');t.value=%s;"
                 "const a=document.querySelector('#gl-announce');if(a&&a.checked)a.click();return true})()"
                 % json.dumps(title))
    await F.wait(d, "!document.querySelector('#gl-go').disabled", 10, 'Announce and Stream enabled')
    n = len(await d.b.js('window.__toastLog||[]'))
    if before_press:
        await before_press()
    await d.b.js("document.querySelector('#gl-go').click();true")
    return n


async def ingest(d):
    return await d.b.js("(async()=>{await __PC.ensureAiSession?.();return await (await fetch('/api/streams/ingest',"
                        "{headers:{'Authorization':'Bearer tok-'+__PC.me().pubkey}})).json()})()")


def announcements(S, pk):
    return sorted(S.n.home.query({'kinds': [30311], 'authors': [pk]}), key=lambda e: e['created_at'])


def tag(ev, name):
    return next((t[1] for t in ev['tags'] if t[0] == name and len(t) > 1), '')


def latest(S, pk, d=None):
    evs = [e for e in announcements(S, pk) if d is None or tag(e, 'd') == d]
    return evs[-1] if evs else None


async def open_card(d, title):
    card = ("[...document.querySelectorAll('.stream-card')].find(c=>c.querySelector('.stream-title').textContent===%s)"
            % json.dumps(title))
    await F.wait(d, '!!' + card, 40, 'the %r card in the list' % title)
    await d.b.js(card + '.click();true')
    await F.wait(d, "!!document.querySelector('.stream-view') && document.querySelector('.av-title').textContent"
                    ".includes(%s)" % json.dumps(title), 20, 'the %r stream page' % title)


async def say(d, text):
    await d.b.js("(()=>{const i=document.querySelector('#st-chat-inp');i.value=%s;"
                 "document.querySelector('#st-chat-send').click();return true})()" % json.dumps(text))


async def chat_has(d, line, what):
    await F.wait(d, '%s.includes(%s)' % (CHAT, json.dumps(line)), 40, what)


async def press_play(d):
    """Click the player, as a person starts a video (a real pointer press -- a user gesture -- on the <video>). A
    click that lands while the native controls are still fading in is eaten by them, so -- like a person -- click
    again if nothing started."""
    for attempt in range(4):
        r = await d.b.js("(()=>{const v=document.querySelector('#st-video');v.scrollIntoView({block:'center'});"
                         "const r=v.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,p:v.paused}})()")
        if not r['p']:
            return
        for t in ('mousePressed', 'mouseReleased'):
            await d.b.call('Input.dispatchMouseEvent', {'type': t, 'x': r['x'], 'y': r['y'], 'button': 'left',
                                                        'clickCount': 1})
        try:
            await F.wait(d, "!document.querySelector('#st-video').paused", 3, 'the player playing after a click')
            return
        except AssertionError:
            continue
    st = await d.b.js("(()=>{const v=document.querySelector('#st-video');return {paused:v.paused,ended:v.ended,"
                      "rs:v.readyState,err:v.error&&v.error.code,t:v.currentTime,dur:v.duration,src:v.currentSrc}})()")
    raise AssertionError(('the player would not start after four clicks', st))


async def advancing(d, what):
    t1 = await d.b.js("document.querySelector('#st-video').currentTime")
    await asyncio.sleep(3)
    t2 = await d.b.js("document.querySelector('#st-video').currentTime")
    assert t2 > t1 + 1, ('%s: the picture is frozen' % what, t1, t2)
    assert await d.b.js(DECODED), '%s: the frame went flat' % what


def mtx_ready(S, token):
    p = S.ctx.mtx.path(token)
    return bool(p and p.get('ready'))


# --------------------------------------------------------------------------------------------- 1 + 2
@scenario
async def a_webcam_go_live_is_watched_chatted_ended_and_replayed(S):
    title = 'Webcam live %d' % S.base
    streamer = await S.device(0, args=FAKE_CAM)
    n0 = await go_live(streamer, title)
    info = await ingest(streamer)
    token = info['token']
    # MediaMTX holds a WebRTC publish on the streamer's own path -- authorised by the app's hook.
    await F.until(lambda: mtx_ready(S, token), 40, 'MediaMTX holding the webcam publish', streamer)
    src = (S.ctx.mtx.path(token) or {}).get('source') or {}
    assert src.get('type') == 'webRTCSession', src
    said = (await streamer.b.js('window.__toastLog||[]'))[n0:]
    assert not any('couldn' in t for t in said), said
    # The announcement, published by the client's own code: live, pointing at this node's HLS proxy for that token.
    pk = S.pk(0)
    await F.until(lambda: latest(S, pk) and tag(latest(S, pk), 'status') == 'live', 30, 'the live 30311 on the relay',
                  streamer)
    live = latest(S, pk)
    d = tag(live, 'd')
    assert tag(live, 'title') == title and d.startswith(token + '-'), live['tags']
    assert tag(live, 'streaming') == '%s/api/streams/hls/%s/index.m3u8' % (S.n.origin, token), live['tags']
    assert await streamer.b.js("!!document.querySelector('#phone-live')"), 'no broadcast controls on screen'

    # A second account finds it, opens it, and the picture plays.
    viewer = await S.device(1)
    await open_streams(viewer)
    await open_card(viewer, title)
    await F.wait(viewer, DECODED, 60, 'the live picture decoded on the viewer\'s screen')
    await press_play(viewer)
    await advancing(viewer, 'live')

    # Chat both ways: the viewer on the stream page, the streamer on their own stream (where Go Live lands them).
    await F.wait(streamer, "!!document.querySelector('#st-chat-inp')", 30, "the streamer's own stream page with chat")
    await say(viewer, 'hello from the audience %d' % S.base)
    await chat_has(streamer, 'hello from the audience %d' % S.base, "the viewer's line on the streamer's chat")
    await asyncio.sleep(1.1)
    await say(streamer, 'thanks for watching %d' % S.base)
    await chat_has(viewer, 'thanks for watching %d' % S.base, "the streamer's reply on the viewer's chat")
    held = S.n.home.query({'kinds': [1311], '#a': ['30311:%s:%s' % (pk, d)]})
    assert sorted(e['pubkey'] for e in held) == sorted([pk, S.pk(1)]), held

    await asyncio.sleep(4)                                # some seconds of footage for the recording
    # 2 -- Stop streaming.
    n1 = len(await streamer.b.js('window.__toastLog||[]'))
    S.ctx.vod_open.clear()            # the recording is still being made while the viewer finds out it ended
    await streamer.b.js("document.querySelector('#pl-stop').click();true")
    # The streamer's own stream page (where Go Live landed them, so where they press Stop) stops saying they are
    # broadcasting AT ONCE -- not at the next 15 s re-read of the announcement.
    await F.wait(streamer, "!/You’re broadcasting/.test(document.querySelector('#feed').innerText) && "
                           "!document.querySelector('.av-title .live-badge')", 3,
                 "the streamer's own page stops claiming the stream is live")
    await F.until(lambda: tag(latest(S, pk, d) or {'tags': []}, 'status') == 'ended', 30,
                  'the announcement flipped to ended', streamer)
    ended = latest(S, pk, d)
    assert tag(ended, 'ends') and tag(ended, 'title') == title and tag(ended, 'starts') == tag(live, 'starts')
    await F.until(lambda: not mtx_ready(S, token), 30, 'MediaMTX no longer holding the publish', streamer)
    said = (await streamer.b.js('window.__toastLog||[]'))[n1:]
    assert any('Stream ended' in t and 'recording' in t for t in said), said
    assert not await streamer.b.js("!!document.querySelector('#phone-live')"), 'the broadcast overlay outlived Stop'
    errs = [e for e in await streamer.b.js('window.__errors||[]') if 'ResizeObserver' not in e]
    assert not errs, errs
    # "You can close the app; it finishes on the server" -- so the streamer closes it. Everything after this (the
    # recording, the replay) must happen with the broadcaster's browser gone.
    S.open.remove(streamer)
    await streamer.__aexit__()

    # The viewer is told it ended -- the page it has open, then the list.
    # ...on the page it has open: the stream page itself turns into the ended stream (no LIVE badge, "ended", the
    # recording being saved) -- never "could not play this stream" over a LIVE badge.
    await F.wait(viewer, "!document.querySelector('.av-title .live-badge') && "
                         "/· ended/.test(document.querySelector('.stream-view').innerText) && "
                         "/Saving your recording|Recorded stream/.test(%s)" % NOTE, 45,
                 "the viewer's open stream turns into the ended stream")
    assert 'Could not play' not in await viewer.b.js(NOTE)
    ended_page_at = time.monotonic()
    S.ctx.vod_open.set()
    await viewer.b.js("document.querySelector('#st-back').click();true")
    card = ("[...document.querySelectorAll('.stream-card')].find(c=>c.querySelector('.stream-title').textContent===%s)"
            % json.dumps(title))
    await F.wait(viewer, '!!%s && !!%s.querySelector(".ended-badge") && !%s.querySelector(".live-badge")'
                 % (card, card, card), 40, 'the list badges it ended')

    # The recording: the real reaper claims MediaMTX's segments, compresses them and stores them on the drive.
    vods = []

    def have_vod():
        nonlocal vods
        try:
            with urllib.request.urlopen('%s/api/streams/vods/by-token/%s' % (S.n.origin, token), timeout=5) as r:
                vods = json.loads(r.read()).get('vods') or []
        except (OSError, ValueError) as e:
            S.ctx.log.append('test vods/by-token: %r' % e)
            return False
        return bool(vods)
    try:
        await F.until(have_vod, 120, 'the recording on the streamer\'s drive', viewer)
    except AssertionError as e:
        from app.services import stream_end_service, stream_vod_service
        rec = [(os.path.relpath(os.path.join(r, f), S.ctx.mtx.rec), os.path.getsize(os.path.join(r, f)),
                round(time.time() - os.path.getmtime(os.path.join(r, f)), 1))
               for r, _d, fs in os.walk(S.ctx.mtx.rec) for f in fs]
        state = {'rec': rec, 'pp_running': stream_vod_service._pp_running,
                 'processing': list(stream_vod_service._processing),
                 'reaper': repr(stream_end_service._reaper), 'pending': len(stream_end_service._pending)}
        raise AssertionError((e, state, [x for x in S.ctx.log if '[mediamtx]' not in x][-40:],
                              S.ctx.log[-30:]))
    vod = vods[0]
    assert vod['duration_s'] and vod['duration_s'] >= 3, vod
    assert os.path.isfile(os.path.join(S.ctx.blobs, vod['sha256'][:2], vod['sha256'])), 'the VOD bytes are not stored'
    # ...and the viewer plays it as the replay.
    await open_card(viewer, title)
    await F.wait(viewer, NOTE + ".includes('Recorded stream')", 60, 'the replay is offered',)
    await F.wait(viewer, DECODED + " && document.querySelector('#st-video').duration>=3", 60,
                 'the replay decoded on the viewer\'s screen')
    await press_play(viewer)
    await advancing(viewer, 'replay')
    # ...and KEEPS playing. The ended page re-checks for its recording 20 s after it was drawn; that timer must not
    # re-render a page opened since, under a replay somebody is watching (it reset the video to 0, paused).
    await viewer.b.js("document.querySelector('#st-video').__watched=1;true")
    await asyncio.sleep(max(0.0, ended_page_at + 24 - time.monotonic()))
    st = await viewer.b.js("(()=>{const v=document.querySelector('#st-video');"
                           "return {same:v.__watched===1,paused:v.paused,t:v.currentTime}})()")
    assert st['same'] and (not st['paused'] or st['t'] > 3), ('the replay was restarted under the viewer', st)
    src = await viewer.b.js("document.querySelector('#st-video').currentSrc||''")
    assert vod['sha256'] in src, ('the replay is not the recording', src)
    errs = [e for e in await viewer.b.js('window.__errors||[]') if 'ResizeObserver' not in e]
    assert not errs, errs


def test_a_webcam_go_live_is_watched_chatted_ended_and_replayed(ran):
    ran('a_webcam_go_live_is_watched_chatted_ended_and_replayed')


# --------------------------------------------------------------------------------------------- 4 (tab dies)
@scenario
async def the_server_ends_a_stream_whose_tab_died(S):
    title = 'Tab dies %d' % S.base
    streamer = await S.device(0, args=FAKE_CAM)
    await go_live(streamer, title)
    token = (await ingest(streamer))['token']
    pk = S.pk(0)
    await F.until(lambda: mtx_ready(S, token), 40, 'MediaMTX holding the webcam publish', streamer)
    await F.until(lambda: latest(S, pk) and tag(latest(S, pk), 'status') == 'live', 30, 'the live 30311', streamer)
    d = tag(latest(S, pk), 'd')
    # The ended twin is parked with the server (the client's sentinel), or nothing could end it.
    from app.services import stream_end_service, user_settings_table
    uid = dict.__getitem__(S.ctx.users, pk)
    await F.until(lambda: asyncio.run_coroutine_threadsafe(
        user_settings_table.aget(None, uid, stream_end_service.SENTINEL_KEY), S.ctx.api.loop).result(10), 20,
        'the parked "ended" event', streamer)
    # The tab dies: the browser is gone, nobody presses Stop.
    S.open.remove(streamer)
    await streamer.__aexit__()
    await F.until(lambda: not mtx_ready(S, token), 60, 'MediaMTX noticing the publisher is gone')
    await F.until(lambda: tag(latest(S, pk, d) or {'tags': []}, 'status') == 'ended', 60,
                  'the server published the parked "ended" event')
    ev = latest(S, pk, d)
    assert tag(ev, 'client') == 'PosterChan', ev['tags']           # the sentinel, signed by the streamer at go-live (NIP-89 name)
    viewer = await S.device(1)
    await open_streams(viewer)
    card = ("[...document.querySelectorAll('.stream-card')].find(c=>c.querySelector('.stream-title').textContent===%s)"
            % json.dumps(title))
    await F.wait(viewer, '!!%s && !!%s.querySelector(".ended-badge")' % (card, card), 40,
                 'a fresh viewer sees it ended, not live')


def test_the_server_ends_a_stream_whose_tab_died(ran):
    ran('the_server_ends_a_stream_whose_tab_died')


# --------------------------------------------------------------------------------------------- 3
def _rtmp_publish(S, path, query, seconds=4):
    """ffmpeg publishing a test pattern over RTMP, as OBS does. Returns (exit code, MediaMTX held the path)."""
    url = 'rtmp://127.0.0.1:%d/%s%s' % (S.ctx.mtx.port('rtmp'), path, ('?' + query) if query else '')
    p = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-re', '-f', 'lavfi', '-i',
                          'testsrc=size=320x240:rate=15', '-t', str(seconds), '-c:v', 'libx264', '-preset', 'ultrafast',
                          '-tune', 'zerolatency', '-g', '15', '-pix_fmt', 'yuv420p', '-f', 'flv', url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    held = False
    deadline = time.monotonic() + seconds + 10
    while p.poll() is None and time.monotonic() < deadline:
        held = held or mtx_ready(S, path)
        time.sleep(.2)
    if p.poll() is None:
        p.kill()
    p.wait(5)
    return p.returncode, held


def _whip_direct(S, path, key, offer):
    url = 'http://127.0.0.1:%d/%s/whip?key=%s' % (S.ctx.mtx.port('webrtc'), path, key)
    req = urllib.request.Request(url, data=offer.encode(), method='POST', headers={'Content-Type': 'application/sdp'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@scenario
async def mediamtx_refuses_forged_and_borrowed_keys(S):
    alice, bob = await S.device(0, args=FAKE_CAM), await S.device(1)
    a, b = await ingest(alice), await ingest(bob)
    a_key = a['stream_key'].split('key=', 1)[1]
    b_key = b['stream_key'].split('key=', 1)[1]
    loop = asyncio.get_running_loop()
    seen = len(S.ctx.api.seen)
    # Forged key, Bob's REAL key on Alice's path, Alice's key on a made-up path: MediaMTX refuses all three.
    for path, q in ((a['token'], 'key=sk-' + 'f' * 64), (a['token'], 'key=' + b_key), ('made-up-path', 'key=' + a_key)):
        rc, held = await loop.run_in_executor(None, _rtmp_publish, S, path, q, 3)
        assert not held and rc != 0, ('MediaMTX let this publish through', path, q[:12], rc)
    hooks = [p for m, p in S.ctx.api.seen[seen:] if p == '/api/streams/auth']
    assert len(hooks) >= 3, ('MediaMTX did not ask the app about every publish', S.ctx.api.seen[seen:])
    # The control: the same encoder with Alice's own key on her own path is accepted and held.
    rc, held = await loop.run_in_executor(None, _rtmp_publish, S, a['token'], 'key=' + a_key, 4)
    assert held, 'the owner\'s own key was refused by MediaMTX -- the refusals above prove nothing'
    await F.until(lambda: not mtx_ready(S, a['token']), 30, 'the control publish ended')
    # WHIP straight to MediaMTX (bypassing the app's proxy, as an attacker on the network would): a real browser
    # offer with a forged key and with Bob's key is refused by MediaMTX itself.
    offer = await alice.b.js("(async()=>{const s=await navigator.mediaDevices.getUserMedia({video:true,audio:true});"
                             "const pc=new RTCPeerConnection();s.getTracks().forEach(t=>pc.addTrack(t,s));"
                             "const o=await pc.createOffer();await pc.setLocalDescription(o);"
                             "s.getTracks().forEach(t=>t.stop());const sdp=pc.localDescription.sdp;pc.close();return sdp})()")
    for key in ('sk-' + 'e' * 64, b_key):
        st = await loop.run_in_executor(None, _whip_direct, S, a['token'], key, offer)
        assert st in (400, 401, 403), ('MediaMTX accepted a WHIP publish with a key that is not the owner\'s', st)
    assert not mtx_ready(S, a['token'])
    # And through the app's own WHIP proxy, Bob cannot publish to Alice's token at all.
    st = await bob.b.js("(async()=>(await fetch('/api/streams/whip/%s',{method:'POST',headers:{'Content-Type':"
                        "'application/sdp','Authorization':'Bearer tok-'+__PC.me().pubkey},body:%s})).status)()"
                        % (a['token'], json.dumps(offer)))
    assert st == 403, st


def test_mediamtx_refuses_forged_and_borrowed_keys(ran):
    ran('mediamtx_refuses_forged_and_borrowed_keys')


# --------------------------------------------------------------------------------------------- 5 (the VoD FILE)
def _rtmp_publish_av(S, path, query, seconds):
    """OBS-shaped: a moving test pattern AND a 440 Hz tone (H.264 + AAC) over RTMP for `seconds`."""
    url = 'rtmp://127.0.0.1:%d/%s%s' % (S.ctx.mtx.port('rtmp'), path, ('?' + query) if query else '')
    p = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-re',
                          '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=15',
                          '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                          '-t', str(seconds), '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                          '-g', '15', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '64k', '-f', 'flv', url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        p.wait(seconds + 20)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(5)
    return p.returncode


def _probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type,codec_name',
                          '-of', 'json', path], capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout or '{}')


def _top_level_boxes(path):
    """The MP4's top-level box types, in file order (moov before mdat = playable while it downloads)."""
    import struct
    boxes = []
    with open(path, 'rb') as f:
        while len(boxes) < 64:
            head = f.read(8)
            if len(head) < 8:
                break
            size, kind = struct.unpack('>I4s', head)
            if size == 1:
                size = struct.unpack('>Q', f.read(8))[0]
                f.seek(size - 16, 1)
            elif size == 0:
                boxes.append(kind.decode('latin-1'))
                break
            else:
                f.seek(size - 8, 1)
            boxes.append(kind.decode('latin-1'))
    return boxes


@scenario
async def an_obs_stream_that_reconnects_becomes_one_complete_vod(S):
    """The FINAL FILE of a stream: an OBS broadcast that drops and reconnects inside one session (two recording
    segments, joined AND compressed in one pass by stream_vod_service._prepare_upload) is stored as ONE video with
    BOTH its picture and its sound, as long as the two parts together, laid out to play while it downloads (moov
    before mdat), its bytes the sha256 it is named by, and decodable to its last second."""
    await _reconnecting_vod(S, 0, 'Reconnecting %d' % S.base)


@scenario
async def a_vod_whose_compression_fails_is_still_the_whole_broadcast(S):
    """The same, when compression cannot run (every encoder failing, the timeout): the recording falls back to the
    plain -c copy concat, and THAT file must be just as complete -- "compression must never cost the user their
    recording" is only true if the fallback is the whole recording."""
    from app.services import media_service

    def broken(*a, **k):
        raise RuntimeError('every encoder failed (test)')
    real = media_service.compress_video_file
    S.ctx.mp.setattr(media_service, 'compress_video_file', broken)   # put back by hand: mp is the whole world's
    try:
        await _reconnecting_vod(S, 1, 'Fallback %d' % S.base)
    finally:
        S.ctx.mp.setattr(media_service, 'compress_video_file', real)


async def _reconnecting_vod(S, who, title):
    import hashlib
    loop = asyncio.get_running_loop()
    d = await S.device(who)
    info = await ingest(d)
    token, key = info['token'], info['stream_key'].split('key=', 1)[1]
    await go_live(d, title, source='obs')
    pk = S.pk(who)
    await F.until(lambda: latest(S, pk) and tag(latest(S, pk), 'title') == title, 30, 'the OBS stream announced', d)
    first, gap, second = 6, 2, 5                      # gap < the reaper's quiet window: the SAME session
    rc1 = await loop.run_in_executor(None, _rtmp_publish_av, S, token, 'key=' + key, first)
    await asyncio.sleep(gap)
    rc2 = await loop.run_in_executor(None, _rtmp_publish_av, S, token, 'key=' + key, second)
    assert rc1 == 0 and rc2 == 0, ('the RTMP publishes were refused', rc1, rc2)
    segs = [f for r, _d, fs in os.walk(S.ctx.mtx.rec) for f in fs if f.endswith('.mp4') and token in r]
    vods = []

    def have_vod():
        nonlocal vods
        try:
            with urllib.request.urlopen('%s/api/streams/vods/by-token/%s' % (S.n.origin, token), timeout=5) as r:
                vods = json.loads(r.read()).get('vods') or []
        except (OSError, ValueError):
            return False
        return bool(vods)
    await F.until(have_vod, 150, 'the reconnected stream saved as a VoD (segments seen: %d)' % len(segs), d)
    assert len(vods) == 1, ('one session must be ONE recording, not one per reconnect', vods)
    vod = vods[0]
    path = os.path.join(S.ctx.blobs, vod['sha256'][:2], vod['sha256'])
    assert os.path.isfile(path), 'the VoD bytes are not stored'
    data = open(path, 'rb').read()
    assert hashlib.sha256(data).hexdigest() == vod['sha256'], 'the stored bytes are not the sha256 they are named by'
    meta = _probe(path)
    kinds = sorted({s.get('codec_type') for s in meta.get('streams', [])})
    assert kinds == ['audio', 'video'], ('the VoD lost its picture or its sound', meta.get('streams'))
    dur = float((meta.get('format') or {}).get('duration') or 0)
    total = first + second
    assert total - 2.5 <= dur <= total + gap + 2.5, ('the VoD is not as long as the broadcast (both parts)', dur, total)
    assert abs(float(vod['duration_s']) - dur) <= 2.5, ('the listed duration is not the file\'s', vod['duration_s'], dur)
    boxes = _top_level_boxes(path)
    assert 'moov' in boxes and 'mdat' in boxes and boxes.index('moov') < boxes.index('mdat'), \
        ('the VoD cannot play until it has fully downloaded (moov after mdat)', boxes)
    print('[vod] segments=%d duration=%.1fs (broadcast %ds + %ds) streams=%s boxes=%s'
          % (len(segs), dur, first, second, kinds, boxes[:6]))
    tail = subprocess.run(['ffmpeg', '-v', 'error', '-sseof', '-1', '-i', path, '-f', 'null', '-'],
                          capture_output=True, text=True, timeout=60)
    assert tail.returncode == 0 and not tail.stderr.strip(), ('the end of the VoD does not decode', tail.stderr[-300:])


def test_an_obs_stream_that_reconnects_becomes_one_complete_vod(ran):
    ran('an_obs_stream_that_reconnects_becomes_one_complete_vod')


def test_a_vod_whose_compression_fails_is_still_the_whole_broadcast(ran):
    ran('a_vod_whose_compression_fails_is_still_the_whole_broadcast')


# --------------------------------------------------------------------------------------------- 4 (cannot publish)
@scenario
async def a_go_live_that_cannot_publish_says_so_and_announces_nothing(S):
    """Three ways the publish fails AFTER the Go Live sheet opened, each told in words, none leaving an announcement."""
    from app.services import api_key_store, user_table
    from app.services.relay_reader import Unavailable
    loop = asyncio.get_running_loop()

    async def attempt(i, title, before_press, said_re, what, source='cam'):
        d = await S.device(i, args=FAKE_CAM)
        pk = S.pk(i)
        before = len(announcements(S, pk))
        await open_streams(d)
        token = (await ingest(d))['token']
        n = await go_live(d, title, source=source, before_press=before_press)
        await F.wait(d, "(window.__toastLog||[]).slice(%d).some(t=>/couldn.t (go live|announce)|you.re live/i.test(t))"
                     % n, 40, 'the failure said to the streamer (%s)' % what)
        said = [t for t in (await d.b.js('window.__toastLog||[]'))[n:] if re.search('go live|announce|live', t)]
        print(what, '->', said)
        assert any(re.search(said_re, t) for t in said), ('%s: not told plainly' % what, said)
        assert not any('WHIP' in t for t in said), ('%s: a protocol status instead of words' % what, said)
        await asyncio.sleep(3)
        assert len(announcements(S, pk)) == before, ('%s: a live announcement went out for a stream that is not '
                                                     'there' % what, announcements(S, pk)[-1]['tags'])
        assert not await d.b.js("!!document.querySelector('#phone-live')"), '%s: a LIVE overlay' % what
        assert not await d.b.js("!!document.querySelector('#stream-end')"), '%s: an "End stream" for nothing' % what
        assert not mtx_ready(S, token)

    # (a) The account's streaming permission is taken away while the sheet is open: the app's WHIP proxy refuses.
    pk0 = S.pk(0)

    async def revoke():
        def go():
            u = S.ctx.users[pk0]
            u.can_stream = False
            user_table.save(None, u)
        await loop.run_in_executor(None, go)
    await attempt(0, 'No permission %d' % S.base, revoke, r'isn.t allowed to stream', 'permission revoked')

    # (b) The app cannot read the stream keys when MediaMTX asks: the auth hook refuses, so MediaMTX refuses.
    real = api_key_store.alookup

    async def unreadable(key):
        raise Unavailable('key table unreadable (test)')

    async def break_keys():
        S.ctx.mp.setattr(api_key_store, 'alookup', unreadable)
    try:
        await attempt(1, 'Keys unreadable %d' % S.base, break_keys, r'stream key', 'auth hook could not read keys')
    finally:
        S.ctx.mp.setattr(api_key_store, 'alookup', real)

    # (c) MediaMTX is down -- from the webcam, and (d) announcing for OBS, which has nowhere to stream to either.
    async def down():
        await loop.run_in_executor(None, S.ctx.mtx.down)
    try:
        await attempt(2, 'Server down %d' % S.base, down, r'stream server isn.t answering', 'MediaMTX down')
        await attempt(3, 'OBS server down %d' % S.base, None, r'couldn.t announce.*stream server isn.t answering',
                      'MediaMTX down, OBS', source='obs')
    finally:
        S.ctx.mtx.up()


def test_a_go_live_that_cannot_publish_says_so_and_announces_nothing(ran):
    ran('a_go_live_that_cannot_publish_says_so_and_announces_nothing')
