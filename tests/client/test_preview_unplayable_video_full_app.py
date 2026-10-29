"""A video this browser cannot decode says which format it is, and on PosterChanOS plays in VLC.

"trying to open video file in browser from photos synced says: This browser cannot play that format" (2026-10-29).
A phone records HEVC more often than not, and neither Chromium nor Firefox on Linux decodes it. The shipped preview.js
in the bundled client, with an MP4 that declares `hvc1` (Chrome cannot play it, so the error path is the real one) and
the desktop's bridge stood in; desktop/player.js itself is run under node with a fake spawn.
"""
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop

ROOT = Path(__file__).resolve().parents[2]
CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# An MP4-shaped file whose sample description is HEVC (`hvc1`): enough for the codec probe, undecodable by Chrome.
HEVC = r"""(()=>{const parts=['\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00isomhvc1','\x00\x00\x00\x10moov\x00\x00\x00\x08hvc1'];
 const s=parts.join('')+'x'.repeat(4000);const u=new Uint8Array(s.length);for(let i=0;i<s.length;i++)u[i]=s.charCodeAt(i)&255;
 return new Blob([u],{type:'video/mp4'});})()"""


async def _open(b, shell):
    await desktop.login(b)
    if shell:
        await b.js("window.__played=[];window.pcShell=Object.assign(window.pcShell||{},{openInPlayer:async(buf,n)=>{__played.push([n,buf.byteLength]);return 'opened';}});true")
    await b.js("PCPreview.open({name:'VID_20261029.mp4',blob:%s});true" % HEVC)
    await b.until("!!document.querySelector('.pv-host .pv-body .empty')")
    await b.until("/HEVC/.test(document.querySelector('.pv-host .pv-body').textContent)")


@CHROME
def test_on_posterchanos_it_names_hevc_and_plays_in_vlc():
    got = {}

    async def check(b):
        await _open(b, True)
        got['text'] = await b.js("document.querySelector('.pv-host .pv-body').textContent")
        await b.js("document.querySelector('.pv-player').click();true")
        await b.until("__played.length===1")
        got['played'] = await b.js("__played[0]")
        await b.until("document.querySelector('.pv-player').textContent==='Opened in VLC'")

    asyncio.run(desktop.with_browser('online', '', check))
    assert 'This video is HEVC (H.265)' in got['text'], got
    assert got['played'][0] == 'VID_20261029.mp4' and got['played'][1] > 4000, ("VLC was not handed the video's bytes", got)


@CHROME
def test_in_a_browser_it_names_the_format_and_says_what_to_do():
    got = {}

    async def check(b):
        await _open(b, False)
        got['text'] = await b.js("document.querySelector('.pv-host .pv-body').textContent")
        got['button'] = await b.js("!!document.querySelector('.pv-player')")

    asyncio.run(desktop.with_browser('online', '', check))
    assert 'HEVC' in got['text'] and 'real player' in got['text'] and not got['button'], got


@pytest.mark.skipif(shutil.which('node') is None, reason='node')
def test_the_desktop_writes_a_private_copy_starts_vlc_and_cleans_up(tmp_path):
    js = r"""
const P = require(%s), fs = require('fs'), path = require('path'), EE = require('events');
const root = %s; const runs = [];
const spawnFn = (cmd, args, opts) => { const c = new EE(); c.pid = 4242; c.unref = () => {}; runs.push([cmd, args, opts.detached]);
  setTimeout(() => c.emit('spawn'), 5); setTimeout(() => c.emit('exit', 0), 40); return c; };
(async () => {
  const r = await P.open(new Uint8Array([1,2,3,4]).buffer, '../../etc/evil name.mp4', { root, spawnFn });
  const dir = fs.readdirSync(root).find(d => d.startsWith(P.PREFIX));
  const file = runs[0][1][1];
  const st = { r, cmd: runs[0][0], dashdash: runs[0][1][0], detached: runs[0][2], inside: path.dirname(file) === path.join(root, dir),
    base: path.basename(file), bytes: [...fs.readFileSync(file)], dirMode: (fs.statSync(path.join(root, dir)).mode & 0o777).toString(8),
    fileMode: (fs.statSync(file).mode & 0o777).toString(8) };
  await new Promise(r => setTimeout(r, 80));
  st.cleaned = !fs.readdirSync(root).some(d => d.startsWith(P.PREFIX));
  fs.mkdirSync(path.join(root, P.PREFIX + 'stale'));                       // a crash left one behind
  st.swept = P.sweep(root);
  console.log(JSON.stringify(st));
})();
""" % (json.dumps(str(ROOT / 'desktop/player.js')), json.dumps(str(tmp_path)))
    r = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    st = json.loads(r.stdout)
    assert st['r'] == 'opened' and st['cmd'] == 'vlc' and st['dashdash'] == '--' and st['detached'], st
    assert st['inside'] and st['base'] == 'evil name.mp4' and st['bytes'] == [1, 2, 3, 4], st
    assert st['dirMode'] == '700' and st['fileMode'] == '600', st
    assert st['cleaned'] and st['swept'] == 1, st


def test_the_bridge_is_shell_only():
    main = (ROOT / 'desktop/main.js').read_text()
    h = main[main.index("ipcMain.handle('pc:play-in-player'"):][:300]
    assert 'SHELL_MODE' in h and 'fromOurPage(e)' in h
    assert 'media-video/vlc' in (ROOT / 'os/gentoo.sh').read_text()
