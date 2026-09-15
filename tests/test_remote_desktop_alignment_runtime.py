"""Exercise the shipped viewer handlers, including capture and resize, in a browser."""
from pathlib import Path

import asyncio
import json
import shutil
import subprocess
import tempfile
from contextlib import contextmanager

import pytest
from websockets.sync.client import connect
from tests.client.test_desktop_offline_full_app import wait_browser_port, wait_browser_target


@contextmanager
def browser_page(tmp_path):
    chrome = shutil.which('google-chrome-stable') or shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome unavailable')
    profile = tmp_path / 'profile'
    chrome_log = tempfile.TemporaryFile()
    proc = subprocess.Popen([chrome,'--headless=new','--no-sandbox','--disable-gpu',
        '--remote-debugging-port=0','--user-data-dir='+str(profile),'about:blank'],
        stdout=chrome_log,stderr=chrome_log)
    try:
        port = asyncio.run(wait_browser_port(proc, profile/'DevToolsActivePort', chrome_log))
        target = asyncio.run(wait_browser_target(proc, port, chrome_log))
        with connect(target) as ws:
            class Page:
                seq=0
                def call(self,method,params):
                    self.seq+=1
                    ws.send(json.dumps({'id':self.seq,'method':method,'params':params}))
                    while True:
                        result=json.loads(ws.recv(timeout=10))
                        if result.get('id')==self.seq:
                            assert 'error' not in result,result
                            return result['result']
                def evaluate(self,code):
                    if code.startswith('() =>'):
                        code='('+code+')()'
                    result=self.call('Runtime.evaluate',{'expression':code,'returnByValue':True,'awaitPromise':True,'userGesture':True})
                    assert 'exceptionDetails' not in result,result
                    return result['result'].get('value')
                def resize(self,width,height,dpr=1):
                    self.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':dpr,'mobile':False})
                def box(self,selector):
                    return self.evaluate('document.querySelector('+json.dumps(selector)+').getBoundingClientRect().toJSON()')
            yield Page()
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        chrome_log.close()


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('remote_width,remote_height,dpr', [(3840,2160,1),(1920,1080,2),(1280,1024,1),(1080,1920,2)])
def test_viewer_capture_scales_motion_and_releases_input(tmp_path,remote_width,remote_height,dpr):
    app = (ROOT / 'static/js/client/app.js').read_text()
    code = app[app.index('  function _rdVideoPoint('):app.index('  // getUserMedia failures')]
    css = (ROOT / 'static/css/client.css').read_text()
    with browser_page(tmp_path) as page:
        page.resize(1000,800,dpr)
        html = ('<style>' + css + '</style><div class="call-overlay rd vid control-on">'
                         '<div class="call-head">Remote Desktop</div>'
                         '<video class="call-remote" id="v"></video>'
                         '<div class="call-actions"><button id="local">Stop control</button></div></div>')
        page.evaluate('document.documentElement.innerHTML='+json.dumps(html))
        page.evaluate('''() => {
          window._call={remoteDesktop:true,caller:false,controlGranted:true,remoteGeometry:{width:3840,height:2160}};
          window.sent=[];window._rdSend=m=>sent.push(m.e);window._RD_KEYS={KeyA:30,ShiftLeft:42};
        }''')
        page.evaluate('_call.remoteGeometry='+json.dumps({'width':remote_width,'height':remote_height}))
        page.evaluate(code + '\n_rdBindViewer(document.getElementById("v"));')
        # Real CSS layout reserves controls instead of obscuring the remote screen edges.
        box = page.box('#v')
        controls = page.box('.call-actions')
        assert box['y'] + box['height'] <= controls['y']
        # The browser owns pointer lock. Use deterministic synthetic deltas once acquired.
        click={'x':box['x']+box['width']/2,'y':box['y']+box['height']/2,'button':'left','clickCount':1}
        page.call('Input.dispatchMouseEvent',dict(click,type='mousePressed'))
        page.call('Input.dispatchMouseEvent',dict(click,type='mouseReleased'))
        assert page.evaluate('document.pointerLockElement===document.getElementById("v")')
        page.evaluate('sent.length=0')
        page.evaluate('v.dispatchEvent(new MouseEvent("pointermove",{movementX:100,movementY:50,bubbles:true}))')
        point = page.evaluate('sent.at(-1)')
        scale = min(box['width']/remote_width,box['height']/remote_height)
        assert abs(point['x'] - (.5+100/(remote_width*scale))) < .001
        assert abs(point['y'] - (.5+50/(remote_height*scale))) < .001
        page.resize(700,1000,dpr)
        box2 = page.box('#v')
        page.evaluate('v.dispatchEvent(new MouseEvent("pointermove",{movementX:50,movementY:0,bubbles:true}))')
        point2 = page.evaluate('sent.at(-1)')
        assert abs(point2['x'] - (point['x']+50/(remote_width*min(box2['width']/remote_width,box2['height']/remote_height)))) < .001
        # Loss of capture and blur release the actual held buttons and keys.
        page.evaluate('''() => {
          v.dispatchEvent(new PointerEvent('pointerdown',{button:2,pointerId:9}));
          document.dispatchEvent(new KeyboardEvent('keydown',{code:'ShiftLeft',bubbles:true}));
          window.dispatchEvent(new Event('blur'));
        }''')
        assert page.evaluate('document.pointerLockElement===null')
        events = page.evaluate('sent')
        assert {'type':'key','code':42,'down':False} in events
        assert any(e['type']=='button' and e['button']==2 and not e['down'] for e in events)
        page.evaluate('document.getElementById("local").focus()')
        page.evaluate('sent.length=0')
        page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{code:'KeyA',bubbles:true}))")
        assert page.evaluate('sent') == []
        # Browser denial still permits absolute drag/click control and cancellation releases it.
        page.evaluate("v.requestPointerLock=()=>Promise.reject(new Error('denied'))")
        page.evaluate("v.dispatchEvent(new PointerEvent('pointerdown',{button:0,pointerId:7,pointerType:'mouse',clientX:350,clientY:400}))")
        page.evaluate("v.dispatchEvent(new PointerEvent('pointercancel',{pointerId:7}))")
        assert page.evaluate("sent.filter(e=>e.type==='button').map(e=>e.down)") == [True,False]
        page.evaluate('sent.length=0')
        page.evaluate('_rdViewerCleanup()')
        page.evaluate("v.dispatchEvent(new PointerEvent('pointermove',{clientX:350,clientY:400}))")
        assert page.evaluate('sent') == []


def test_phone_touch_requires_grant_and_releases_on_cancel(tmp_path):
    app = (ROOT / 'static/js/client/app.js').read_text()
    code = app[app.index('  function _rdVideoPoint('):app.index('  // getUserMedia failures')]
    with browser_page(tmp_path) as page:
        page.resize(390, 844)
        page.evaluate("document.body.innerHTML='<video id=\"v\" style=\"width:390px;height:220px;touch-action:none\"></video>'")
        page.evaluate('''window._call={remoteDesktop:true,caller:false,controlGranted:false,remoteGeometry:{width:1920,height:1080}};
window._rdViewerCleanup=null;window._RD_KEYS={};window.sent=[];window._rdSend=m=>sent.push(m.e);
window.lockRequests=0;v.requestPointerLock=()=>{lockRequests++;};
window.touch=(name)=>v.dispatchEvent(new PointerEvent(name,{pointerType:'touch',pointerId:8,button:0,clientX:195,clientY:110,bubbles:true,cancelable:true}));''')
        page.evaluate(code + '\n_rdBindViewer(document.getElementById("v"));')
        page.evaluate("touch('pointerdown');touch('pointermove')")
        assert page.evaluate('sent.length') == 0
        page.evaluate("_call.controlGranted=true;touch('pointerdown');touch('pointermove');touch('pointercancel')")
        sent=page.evaluate('sent')
        assert [e['type'] for e in sent] == ['button','absolute','button']
        assert sent[0]['down'] is True and sent[-1]['down'] is False
        assert all(0<=e['x']<=1 and 0<=e['y']<=1 for e in sent)
        assert page.evaluate('lockRequests') == 0
        page.evaluate("_rdViewerCleanup();touch('pointerdown')")
        assert page.evaluate('sent.length') == 3
