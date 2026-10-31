"""The desktop app's half of PBS: hardware encode switched on per OS, and the macOS capture permission readable.

Run: venv-unified/bin/python -m pytest tests/test_desktop_pbs_capture.py

Electron itself cannot run here (no display), so the SHIPPED main.js functions are executed under node with the
Electron objects stubbed, the way tests/test_desktop_display_audio.py runs the display-media handler:

  * Linux gets ONE --enable-features carrying the PipeWire capturer AND Chromium's VA-API encoder features
    (AcceleratedVideoEncoder, VaapiVideoEncoder). Chromium keeps the last of a repeated switch, so a second
    appendSwitch would have silently dropped screen capture on Wayland.
  * Windows (Media Foundation) and macOS (VideoToolbox) encode H.264 in hardware by default: nothing is appended.
  * pc:capture:info answers macOS's Screen Recording permission ('denied' is what PBS explains), 'granted' where no
    such gate exists, and nothing at all to a page that is not ours; pc:capture:open-settings opens the macOS pane
    and nothing elsewhere.
  * preload exposes `pcCapture` inside the our-page block only.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / 'desktop/main.js').read_text(encoding='utf-8')
PRELOAD = (ROOT / 'desktop/preload.js').read_text(encoding='utf-8')
pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')


def _between(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


def _node(script, **subs):
    for k, v in subs.items():
        script = script.replace(k, json.dumps(v))
    r = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def test_linux_turns_on_vaapi_encode_in_the_one_features_switch_and_others_add_nothing():
    wire = _between(MAIN, 'const LINUX_FEATURES', '// ---- Tor')
    got = _node(r'''
const vm=require('node:vm');const out={};
for(const platform of ['linux','win32','darwin']){const sw=[];
  vm.runInNewContext(WIRE+';wireWaylandCapture()',{process:{platform},app:{commandLine:{appendSwitch:(k,v)=>sw.push([k,v])}}});
  out[platform]=sw;}
process.stdout.write(JSON.stringify(out));''', WIRE=wire)
    [(key, value)] = got['linux']
    assert key == 'enable-features'
    feats = value.split(',')
    for f in ('WebRTCPipeWireCapturer', 'AcceleratedVideoEncoder', 'VaapiVideoEncoder'):
        assert f in feats, (f, feats)
    assert got['win32'] == [] and got['darwin'] == []
    assert MAIN.count("appendSwitch('enable-features'") == 1, 'a second enable-features switch would replace the first'


def test_capture_info_reports_the_macos_screen_permission():
    handlers = _between(MAIN, "ipcMain.handle('pc:capture:info'", "// Screen picker: thumbnails")
    feats = _between(MAIN, 'const LINUX_FEATURES', 'function wireWaylandCapture')
    got = _node(r'''
const vm=require('node:vm');
async function run(platform,status,ours){const h={},opened=[];
  const ctx={process:{platform},ipcMain:{handle:(n,f)=>{h[n]=f}},fromOurPage:()=>ours,
    systemPreferences:{getMediaAccessStatus:k=>k==='screen'?status:'x'},shell:{openExternal:async u=>{opened.push(u)}}};
  vm.runInNewContext(FEATS+HANDLERS,ctx);
  return {info:await h['pc:capture:info']({}),open:await h['pc:capture:open-settings']({}),opened};}
(async()=>{process.stdout.write(JSON.stringify({
  macDenied:await run('darwin','denied',true),macOk:await run('darwin','granted',true),
  linux:await run('linux','denied',true),stranger:await run('darwin','denied',false)}));})();''',
                FEATS=feats, HANDLERS=handlers)
    assert got['macDenied']['info']['screen'] == 'denied' and got['macDenied']['open'] is True
    assert 'Privacy_ScreenCapture' in got['macDenied']['opened'][0]
    assert got['macOk']['info']['screen'] == 'granted'
    assert got['linux']['info'] == {'platform': 'linux', 'screen': 'granted',
                                     'features': ['WebRTCPipeWireCapturer', 'AcceleratedVideoEncoder', 'VaapiVideoEncoder']}
    assert got['linux']['open'] is False and got['linux']['opened'] == []
    assert got['stranger']['info'] is None and got['stranger']['open'] is False


def test_the_bridge_is_exposed_to_our_page_only():
    ours = PRELOAD.index('if (isOurPage) {')
    at = PRELOAD.index("exposeInMainWorld('pcCapture'")
    assert at > ours and '\n}\n' not in PRELOAD[ours:at], 'pcCapture must sit inside the our-page block'
    block = _between(PRELOAD, "exposeInMainWorld('pcCapture'", '});')
    assert "'pc:capture:info'" in block and "'pc:capture:open-settings'" in block
