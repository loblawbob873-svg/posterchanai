"""A local file streamed to the desktop's renderer can be abandoned part-way without crashing the app.

"desktop just crashed again, Invalid state: ReadableStream" (2026-10-15). `serveHostFile` (desktop/main.js)
answered `app://posterchan/__hostfile/...` -- what Preview, Files' "This Computer" and the video player read a
local file through -- with `new Response(fs.createReadStream(...))`. When the renderer stops reading (a seek, a
picture that is not one, a window closed while a file loads) the Node-stream bridge enqueues into a closed
stream and throws ERR_INVALID_STATE with nothing to catch it: Electron's main-process error dialog, the
desktop frozen behind it. Real Electron 44, the SHIPPED function sliced out of main.js: 200 rounds of an
image, a video and a fetch abandoned mid-stream must raise nothing, and full and ranged reads must still be
the file's exact bytes. Before the fix this failed in about half the runs of 40 rounds.
"""
import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

from tests.test_native_window_reload_electron import _missing_runtime, _native_popen, _stop_native

ROOT = Path(__file__).resolve().parents[1]
MAIN = r'''
const {app,BrowserWindow,protocol}=require('electron');const fs=require('fs'),path=require('path');
const errs=[];process.on('uncaughtException',e=>{errs.push('UNCAUGHT '+(e.code||'')+' '+e.message);});
process.on('unhandledRejection',e=>{errs.push('UNHANDLED '+(e&&e.code||'')+' '+(e&&e.message));});
protocol.registerSchemesAsPrivileged([{scheme:'app',privileges:{standard:true,secure:true,supportFetchAPI:true,stream:true}}]);
const src=fs.readFileSync(ROOT+'/desktop/main.js','utf8');
const _MIME={'.png':'image/png','.mp4':'video/mp4'};const hostfs=()=>({clean:p=>'/'+p.replace(/^\/+/,'')});
eval(src.slice((src.indexOf('function fileStream(')>=0?src.indexOf('function fileStream('):src.indexOf('function serveHostFile(')),src.indexOf('function serveBundle(')));
app.whenReady().then(async()=>{
 protocol.handle('app',req=>{let rel=decodeURIComponent(new URL(req.url).pathname);
   if(rel.startsWith('/__hostfile/')) return serveHostFile(req,rel);
   return new Response('<html><body>hi</body></html>',{headers:{'content-type':'text/html'}});});
 const win=new BrowserWindow({width:600,height:400,show:true});
 await win.loadURL('app://posterchan/index.html');
 for(let i=0;i<200;i++){
  await win.webContents.executeJavaScript(`(async()=>{const d=document.body;d.innerHTML='';
    const im=new Image();im.src='app://posterchan/__hostfile/'+encodeURIComponent(OUT+'/fake.png');d.append(im);
    const v=document.createElement('video');v.src='app://posterchan/__hostfile/'+encodeURIComponent(OUT+'/big.mp4');v.preload='auto';d.append(v);
    const c=new AbortController();fetch('app://posterchan/__hostfile/'+encodeURIComponent(OUT+'/big.mp4'),{signal:c.signal}).then(r=>{const rd=r.body.getReader();return rd.read().then(()=>c.abort())}).catch(()=>{});
    await new Promise(r=>setTimeout(r,150));v.removeAttribute('src');v.load();d.innerHTML='';return 1})()`);
 }
 const chk=await win.webContents.executeJavaScript(`(async()=>{const u='app://posterchan/__hostfile/'+encodeURIComponent(OUT+'/big.mp4');
   const h=async b=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b))).map(x=>x.toString(16).padStart(2,'0')).join('');
   const full=await (await fetch(u)).arrayBuffer(); const r=await fetch(u,{headers:{range:'bytes=1000-1999999'}});
   const part=await r.arrayBuffer(); return {full:full.byteLength, fullsha:await h(full), status:r.status, part:part.byteLength, partsha:await h(part), want:await h(full.slice(1000,2000000))}})()`);
 errs.push('CHECK '+JSON.stringify({len:chk.full,status:chk.status,part:chk.part,partOk:chk.partsha===chk.want,fullsha:chk.fullsha.slice(0,12)}));
 await win.loadURL('app://posterchan/other.html');
 await new Promise(r=>setTimeout(r,1500));
 console.log('RESULT '+JSON.stringify(errs.slice(0,5))+' n='+errs.length);app.exit(0);
}).catch(e=>{console.error('FAIL',e);app.exit(1)});
'''


def test_an_abandoned_local_file_stream_raises_nothing_and_ranges_are_exact(tmp_path):
    binary = os.environ.get('PC_ELECTRON_BINARY', str(ROOT / 'desktop/node_modules/electron/dist/electron'))
    if not Path(binary).is_file():
        _missing_runtime('Electron binary required')
    if not (shutil.which('wayfire') and shutil.which('Xwayland')):
        _missing_runtime('Wayfire plus Xwayland required')
    out = tmp_path
    (out / 'fake.png').write_text('<!doctype html><html>' + 'x' * 200000 + '</html>')
    (out / 'big.mp4').write_bytes(os.urandom(30_000_000))
    (out / 'main.cjs').write_text(MAIN.replace('ROOT', json.dumps(str(ROOT))).replace('OUT', json.dumps(str(out))))
    rt = out / 'rt'
    rt.mkdir(mode=0o700)
    (out / 'wf.ini').write_text('[core]\nplugins = ipc ipc-rules\nxwayland = false\n')
    env = {**os.environ, 'XDG_RUNTIME_DIR': str(rt), 'WLR_BACKENDS': 'headless', 'WLR_HEADLESS_OUTPUTS': '1',
           'WLR_RENDERER': 'pixman', 'GDK_BACKEND': 'wayland'}
    for k in ('ELECTRON_RUN_AS_NODE', 'WAYLAND_DISPLAY', 'DISPLAY', 'WAYFIRE_SOCKET', 'DBUS_SESSION_BUS_ADDRESS'):
        env.pop(k, None)
    procs = []
    try:
        with (out / 'native.log').open('w') as log:
            wf = _native_popen(['wayfire', '-c', str(out / 'wf.ini')], env=env, stdout=log, stderr=log)
            procs.append(wf)
            deadline = time.monotonic() + 30
            while not [x for x in rt.glob('wayland-*') if not x.name.endswith('.lock')]:
                assert wf.poll() is None and time.monotonic() < deadline, 'compositor did not start'
                time.sleep(.05)
            env['WAYLAND_DISPLAY'] = next(x.name for x in rt.glob('wayland-*') if not x.name.endswith('.lock'))
            rd, wr = os.pipe()
            try:
                procs.append(_native_popen(['Xwayland', '-displayfd', str(wr), '-nolisten', 'tcp', '-ac', '-noreset', '-shm'],
                                           env=env, pass_fds=(wr,), stdout=log, stderr=log))
                os.close(wr)
                wr = None
                assert select.select([rd], [], [], 10)[0], 'Xwayland readiness timeout'
                display = os.read(rd, 32).decode().strip()
            finally:
                os.close(rd)
                if wr is not None:
                    os.close(wr)
            env.update(DISPLAY=':' + display, GDK_BACKEND='x11')
            r = subprocess.run([binary, '--no-sandbox', '--disable-gpu', '--ozone-platform=x11', str(out / 'main.cjs')],
                               env=env, capture_output=True, text=True, timeout=180)
    finally:
        for p in reversed(procs):
            _stop_native(p)
    line = next((l for l in (r.stdout + r.stderr).splitlines() if l.startswith('RESULT ')), None)
    assert line, (r.stdout + r.stderr)[-4000:]
    errs = json.loads(line[len('RESULT '):line.rindex(' n=')])
    check = json.loads(next(e for e in errs if e.startswith('CHECK '))[6:])
    assert [e for e in errs if not e.startswith('CHECK ')] == [], errs
    import hashlib
    assert check['fullsha'] == hashlib.sha256((out / 'big.mp4').read_bytes()).hexdigest()[:12], check
    assert check == {'len': 30000000, 'status': 206, 'part': 1999000, 'partOk': True,
                     'fullsha': check['fullsha']}, check
