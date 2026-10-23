"""Media Center when the media server misbehaves, in the SHIPPED bundle: the screen must say what happened, never sit on
"Loading…", never write anything on a failed read, and never leave an empty black player.

  * the library list answers 503: the server's own sentence and a Retry, and nothing is written; Retry, once the
    server is back, brings the libraries;
  * a library's titles never arrive: the request gives up and SAYS so (no endless "Working…");
  * Play, where the stream is real (a few seconds of HLS made by ffmpeg, served by the same origin) -- the picture
    must actually play, even when the side writes around it fail (the "resume from" progress save and the
    audio/subtitle track list both 503: neither is the movie);
  * Play where the stream itself is unreachable: an explicit "Playback failed", not a black box.

Only the media node's /api/media-center answers are fixtures (stubbed in fetch, as in the other Media Center tests),
and the 20 s request timeout in mediacenter.js is shortened to 1.5 s -- only for timers armed from mediacenter.js.

Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/mediacenter.js=<copy>" serves a different mediacenter.js.
"""
import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

pytestmark = pytest.mark.skipif(not (Path('/opt/google/chrome/chrome').exists() and shutil.which('ffmpeg')),
                                reason='Chrome and ffmpeg required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    for _ in desktop.bundle.__wrapped__():
        root = desktop.BUNDLE_ROOT
        for pair in filter(None, os.environ.get('PC_JOURNEY_OVERRIDE', '').split(',')):
            path, _, src = pair.partition('=')
            shutil.copyfile(src, root / path.lstrip('/'))
        hls = root / 'hls'
        hls.mkdir()
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10', '-f', 'lavfi',
                        '-i', 'sine=frequency=440', '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                        '-hls_time', '1', '-hls_playlist_type', 'vod', str(hls / 'master.m3u8')],
                       check=True, timeout=60)
        yield


FIXTURE = r'''
window.__mcReq=[];
window.__mc={list:'ok',items:'ok',progress:'ok',tracks:'ok',stream:'/hls/master.m3u8'};
(()=>{const st=window.setTimeout;
  window.setTimeout=function(f,d,...a){if(d===20000&&/\/mediacenter\.js/.test(new Error().stack||''))d=1500;return st.call(this,f,d,...a);};})();
(()=>{const inner=window.fetch;
 const ok=(j,s=200)=>new Response(JSON.stringify(j),{status:s,headers:{'Content-Type':'application/json'}});
 const down=()=>ok({detail:'The media server is restarting. Try again in a minute.'},503);
 const never=(opts)=>new Promise((_,rej)=>{const s=opts&&opts.signal;if(s)s.addEventListener('abort',()=>rej(new DOMException('aborted','AbortError')));});
 const items=[{id:'film1',name:'Film One',folder:'.',duration:4,video:true,progress:{}}];
 const lib={id:'L1',name:'Movies',owner:'x',count:1,scanned_at:1,skipped:0,encoder:'cpu',can_manage:true,
   shared_with_me:false,scan:{state:'idle'},revision:'r1',folder:'/m',shared_with:[]};
 window.fetch=async function(url,opts={}){
  const u=String(url);const at=u.indexOf('/api/media-center');
  if(at<0) return inner(url,opts);
  const p=new URL(u.slice(at),'http://x');const path=p.pathname.replace('/api/media-center','');
  const method=opts.method||'GET';__mcReq.push({path,method});
  if(path===''&&method==='GET') return __mc.list==='503'?down():ok({libraries:[lib],can_create:true,profiles:['480p'],viewer:'v',unshared:0});
  if(path==='/roots') return ok({roots:['/m']});
  if(path==='/limits') return ok({server_kbps:20000,max_streams:8,max_transcodes:2,cache_mb:2048});
  if(path.startsWith('/jellyfin-account')) return ok({devices:[],server:''});
  if(/\/folders$/.test(path)) return ok({path:'.',folders:[]});
  if(/\/items$/.test(path)) return __mc.items==='hang'?never(opts):ok({items,scan:{state:'idle'},revision:'r1'});
  if(/\/play\//.test(path)) return ok({url:location.origin+__mc.stream+'?ticket=t1'});
  if(/\/progress\//.test(path)) return __mc.progress==='503'?down():ok({position:0});
  if(/\/tracks\//.test(path)) return __mc.tracks==='503'?down():ok({tracks:[]});
  if(path==='/sessions/stop') return ok({});
  return ok({});
 };})();
'''

async def until(b, expr, timeout=15, what=''):
    loop = asyncio.get_running_loop(); end = loop.time() + timeout
    while loop.time() < end:
        if await b.js(expr): return
        await asyncio.sleep(.1)
    state = await b.js("({video:(v=>v&&{rs:v.readyState,t:v.currentTime,src:v.src,err:v.error&&v.error.code,paused:v.paused,w:v.videoWidth})(document.querySelector('#mc-player')),hls:!!window.Hls,res:performance.getEntriesByType('resource').map(e=>e.name).filter(n=>/hls|m3u8|\\.ts/.test(n)).slice(-5),errors:__errors,status:(document.querySelector('#mc-status')||{}).textContent,"
                       "feed:(document.querySelector('#feed')||{}).innerText?.slice(0,400),req:__mcReq.slice(-8)})")
    raise AssertionError(f'waited {timeout}s for {what or expr}; state={state}')


WRITES = "__mcReq.filter(r=>r.method!=='GET'&&!/\\/sessions\\/stop$/.test(r.path))"
STATUS = "(document.querySelector('#mc-status')||{}).textContent||''"


async def _open_movies(b):
    await until(b, "!!document.querySelector('.mc-library-open')", what='the library list')
    await b.js("document.querySelector('.mc-library-open').click();true")
    await until(b, "!!document.querySelector('#mc-items .mc-tile[data-item=film1]')", what='the titles')


async def _play(b):
    # A REAL pointer click, as a person makes it: it carries the user activation video.play() needs.
    await b.js("document.querySelector('.mc-tile[data-item=film1] .mc-play').scrollIntoView({block:'center'});true")
    r = await b.js("(()=>{const r=document.querySelector('.mc-tile[data-item=film1] .mc-play').getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()")
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': r[0], 'y': r[1], 'button': 'left', 'clickCount': 1})
    await until(b, "!document.querySelector('#mc-playback').hidden", what='the player to open')


PLAYING = "(()=>{const v=document.querySelector('#mc-player');return !!v&&v.readyState>=2&&v.currentTime>0.3&&v.videoWidth>0})()"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_failing_media_server_is_said_out_loud_and_retry_recovers():
    async def check(b):
        await desktop.login(b)
        await b.js("__mc.list='503';__PC.switchView('media-center');true")
        await until(b, "/restarting/.test((document.querySelector('#feed')||{}).innerText||'') && !!document.querySelector('#mc-retry')",
                      what="the server's own sentence and a Retry")
        assert not await b.js("!!document.querySelector('#mc-loading')"), 'still says Loading under the error'
        assert await b.js(WRITES) == [], 'something was written on a failed read'
        # The server is back: Retry brings the libraries without leaving the screen.
        await b.js("__mc.list='ok';document.querySelector('#mc-retry').click();true")
        await _open_movies(b)
        # A library whose titles never arrive: the request gives up and says so, the button is usable again.
        await b.js("__mc.items='hang';document.querySelector('.mc-library-open').click();true")
        await until(b, f"/took too long/.test({STATUS})", what='the stalled title list to be reported')
        assert not await b.js("document.querySelector('.mc-library-open').disabled"), 'the library button stayed disabled'
        assert await b.js(WRITES) == [], 'something was written on a failed read'
        await b.js("__mc.items='ok';document.querySelector('.mc-library-open').click();true")
        await until(b, f"!/took too long/.test({STATUS}) && !!document.querySelector('#mc-items .mc-tile[data-item=film1]')",
                      what='the titles after the server answered again')
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_movie_plays_even_when_the_writes_around_it_fail_and_a_dead_stream_says_so():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('media-center');true")
        await _open_movies(b)
        # The progress save and the track list are down. Neither is the movie: it must still play.
        await b.js("__mc.progress='503';__mc.tracks='503';true")
        await _play(b)
        await until(b, PLAYING, timeout=20, what='the picture to actually play')
        assert 'failed' not in (await b.js(STATUS)).lower(), await b.js(STATUS)
        await b.js("document.querySelector('#mc-player').pause();true")
        # The stream itself is unreachable: an explicit error, never a silent black box.
        await b.js("__mc.progress='ok';__mc.tracks='ok';__mc.stream='/hls/missing.m3u8';true")
        await b.js("document.querySelector('#mc-close-player').click();true")
        await until(b, "document.querySelector('#mc-playback').hidden", what='the player to close')
        await _play(b)
        await until(b, f"/Playback failed/.test({STATUS})", timeout=20, what='"Playback failed" for a dead stream')
        assert not await b.js(PLAYING)
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
