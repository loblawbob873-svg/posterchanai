#!/usr/bin/env python3
"""Exercise shipped term.js and xterm with a private GNU screen/Codex PTY, without submitting a prompt.
This diagnostic never attaches to, types in, or terminates an existing user session.
"""
import asyncio
import codecs
import fcntl
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
PAGE = r'''<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="/static/vendor/xterm/xterm.css"><link rel="stylesheet" href="/static/css/client.css">
<style>html,body{margin:0;height:100%}.app{height:100dvh;display:flex;flex-direction:column}#feed{flex:1;min-height:0;display:flex;flex-direction:column}</style>
<div class="app"><div id="feed"></div></div>
<script src="/static/vendor/xterm/xterm.js"></script><script src="/static/vendor/xterm/fit.js"></script>
<script>
const OldTerminal=window.Terminal;window.Terminal=function(opts){return window.TEST_TERM=new OldTerminal(opts);};
const RealWS=WebSocket;window.WebSocket=function(){return new RealWS('ws://127.0.0.1:WS_PORT');};
window.__PC_TOKEN__='fixture';const $=(s,r)=>(r||document).querySelector(s);
window.__PC={$,$$:(s,r)=>[...(r||document).querySelectorAll(s)],VIEW:'terminal',enc:s=>String(s),toast:()=>{},
 ensureAiSession:async()=>{},uiConfirm:async()=>true,uiPrompt:async()=>'',
 authFetch:async u=>({ok:true,status:200,json:async()=>u.includes('/hosts')?{ok:true,available:true,hosts:[{name:'fixture',label:'Private screen',keyed:true}]}:{ok:true,keeper:true,sessions:[]}})};
window.snapshot=()=>{const b=TEST_TERM.buffer.active;return Array.from({length:b.length},(_,i)=>b.getLine(i)?.translateToString(true)||'').join('\n');};
</script><script src="/static/js/client/term.js"></script>
<script>PCTerm.render(document.querySelector('#feed'));</script>'''

async def main():
    import websockets
    chrome=shutil.which('google-chrome-stable') or shutil.which('chromium')
    codex=shutil.which('codex')
    if not chrome or not codex or not shutil.which('screen'):
        print('SKIP: Chrome, Codex and screen are required');return 2
    scratch=Path(tempfile.mkdtemp(prefix='pc-screen-check-'))
    name='pc-regression-'+scratch.name
    config=scratch/'screenrc'
    config.write_text('startup_message off\nterm '+os.environ.get('PC_SCREEN_INNER_TERM','xterm-256color')+'\n')
    child=None;master=None; sent=[];raw=bytearray()
    async def handler(ws):
        nonlocal child,master
        msg=json.loads(await ws.recv())
        assert msg['t']=='open'
        child,master=pty.fork()
        if child==0:
            os.environ['TERM']='xterm-256color'
            fcntl.ioctl(0,termios.TIOCSWINSZ,struct.pack('HHHH',msg['rows'],msg['cols'],0,0))
            os.chdir(str(ROOT))
            os.execvp('screen',['screen','-c',str(config),'-S',name,'-U',codex,'-C',os.environ.get('PC_SCREEN_CODEX_CWD',str(ROOT)),'--no-alt-screen'])
        os.set_blocking(master,False)
        await ws.send(json.dumps({'t':'ready','sid':'fixture-screen','host':'fixture'}))
        async def output():
            seq=0;decoder=codecs.getincrementaldecoder('utf-8')('replace')
            while True:
                try: data=os.read(master,65536)
                except BlockingIOError:
                    await asyncio.sleep(.01);continue
                except OSError:return
                if not data:return
                raw.extend(data);text=decoder.decode(data)
                seq+=len(data)
                if text: await ws.send(json.dumps({'t':'out','d':text,'seq':seq}))
        pump=asyncio.create_task(output())
        try:
            async for frame in ws:
                m=json.loads(frame)
                if m['t']=='in':sent.append(m['d']);os.write(master,m['d'].encode())
                elif m['t']=='size':fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',m['rows'],m['cols'],0,0))
        finally:pump.cancel()
    server=await websockets.serve(handler,'127.0.0.1',0)
    ws_port=server.sockets[0].getsockname()[1]
    class HTTP(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT),**kwargs)
        def do_GET(self):
            if self.path=='/':
                body=PAGE.replace('WS_PORT',str(ws_port)).encode();self.send_response(200);self.end_headers();self.wfile.write(body)
            else:super().do_GET()
        def log_message(self,*args):pass
    http=ThreadingHTTPServer(('127.0.0.1',0),HTTP);threading.Thread(target=http.serve_forever,daemon=True).start()
    port=int(os.environ.get('PC_CHECK_PORT','9550'))
    process=subprocess.Popen([chrome,'--headless=new','--no-sandbox','--disable-gpu','--window-size=1280,900',f'--remote-debugging-port={port}',f'--user-data-dir={scratch}/chrome','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        page=None
        for _ in range(80):
            try:page=next(p for p in json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list',timeout=1)) if p['type']=='page');break
            except Exception:await asyncio.sleep(.1)
        if not page:raise RuntimeError('Chrome did not start')
        async with websockets.connect(page['webSocketDebuggerUrl'],max_size=8*1024*1024) as cdp:
            seq=0
            async def call(method,params=None):
                nonlocal seq
                seq+=1; ident=seq;await cdp.send(json.dumps({'id':ident,'method':method,'params':params or {}}))
                while True:
                    result=json.loads(await cdp.recv())
                    if result.get('id')==ident:return result
            async def js(code):
                r=await call('Runtime.evaluate',{'expression':code,'returnByValue':True,'awaitPromise':True})
                return r.get('result',{}).get('result',{}).get('value')
            await call('Page.navigate',{'url':f'http://127.0.0.1:{http.server_address[1]}/'})
            await asyncio.sleep(1)
            await js("document.querySelector('#tty-go').click()")
            await asyncio.sleep(8)
            before=await js('snapshot()')
            if before and 'Update available!' in before:
                await call('Input.dispatchKeyEvent',{'type':'keyDown','key':'ArrowDown','code':'ArrowDown','windowsVirtualKeyCode':40})
                await call('Input.dispatchKeyEvent',{'type':'keyUp','key':'ArrowDown','code':'ArrowDown','windowsVirtualKeyCode':40})
                await call('Input.dispatchKeyEvent',{'type':'keyDown','key':'Enter','code':'Enter','windowsVirtualKeyCode':13,'text':'\r'})
                await call('Input.dispatchKeyEvent',{'type':'keyUp','key':'Enter','code':'Enter','windowsVirtualKeyCode':13})
                await asyncio.sleep(8)
                before=await js('snapshot()')
            print('BEFORE:',before)
            await js('TEST_TERM.focus()')
            await call('Input.insertText',{'text':'terminal-regression-fixture'})
            await asyncio.sleep(1)
            after=await js('snapshot()');print('AFTER:',after)
            for _ in range(7):
                await call('Input.dispatchKeyEvent',{'type':'keyDown','key':'Backspace','code':'Backspace','windowsVirtualKeyCode':8})
                await call('Input.dispatchKeyEvent',{'type':'keyUp','key':'Backspace','code':'Backspace','windowsVirtualKeyCode':8})
                await asyncio.sleep(.06)
            await call('Input.insertText',{'text':'edited'})
            await asyncio.sleep(1)
            edited=await js('snapshot()');print('EDITED:',edited)
            assert edited.count('terminal-regression-edited')==1 and 'terminal-regression-fixture' not in edited, 'editing left duplicate or stale draft text'
            for width in [390,1280,600,1280]:
                await call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':False})
                await asyncio.sleep(1)
                actual=struct.unpack('HHHH',fcntl.ioctl(master,termios.TIOCGWINSZ,b'\0'*8))[:2]
                expected=await js('[TEST_TERM.rows,TEST_TERM.cols]')
                assert list(actual)==expected, f'PTY grid {actual} does not match renderer {expected}'
            resized=await js('snapshot()');print('RESIZED:',resized)
            assert resized.count('terminal-regression-edited')==1, 'resizing duplicated or corrupted the draft'
            (scratch/'output.bin').write_bytes(raw)
            (scratch/'input.json').write_text(json.dumps(sent))
            print('Artifacts:',scratch)
            if not before or not after:raise AssertionError('terminal did not render')
            if ''.join(sent).count('terminal-regression-fixture')!=1:raise AssertionError('input duplicated or lost')
            if after.count('terminal-regression-fixture') != 1:
                raise AssertionError('draft did not render exactly once; startup dialogs are not a typing test')
            print('PASS: one browser insertion reached the private screen PTY and rendered in the Codex draft exactly once')
    finally:
        subprocess.run(['screen','-S',name,'-X','quit'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if child:
            try:os.kill(child,signal.SIGTERM);os.waitpid(child,0)
            except (ProcessLookupError,ChildProcessError):pass
        if master is not None:os.close(master)
        process.terminate();process.wait(timeout=10);http.shutdown();server.close();await server.wait_closed()
    return 0

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
