"""Real Chrome layout and trusted pointer selection through the shipped emoji picker."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest
from websockets.sync.client import connect

ROOT=Path(__file__).resolve().parents[2]


class Browser:
    def __init__(self, socket):
        self.socket=socket
        self.seq=0
        self.session=None

    def command(self, method, params=None):
        self.seq+=1
        message={'id':self.seq,'method':method,'params':params or {}}
        if self.session:
            message['sessionId']=self.session
        self.socket.send(json.dumps(message))
        while True:
            response=json.loads(self.socket.recv(timeout=15))
            if response.get('id')==self.seq:
                assert 'error' not in response,response
                return response.get('result',{})

    def evaluate(self, expression):
        response=self.command('Runtime.evaluate',{'expression':expression,'returnByValue':True,'awaitPromise':True})
        assert 'exceptionDetails' not in response,response
        return response['result'].get('value')

    def click(self, selector, touch=False):
        point=self.evaluate("""(()=>{const e=document.querySelector(SELECTOR),r=e.getBoundingClientRect();
        const x=r.left+r.width/2,y=r.top+r.height/2;
        if(!e.contains(document.elementFromPoint(x,y)))throw Error('Pointer target is clipped or obscured');
        return {x,y};})()""".replace('SELECTOR',json.dumps(selector)))
        if touch:
            self.command('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[point]})
            self.command('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
            return
        for event in ('mousePressed','mouseReleased'):
            self.command('Input.dispatchMouseEvent',{'type':event,'button':'left','clickCount':1,**point})


@pytest.fixture(scope='module')
def chrome(tmp_path_factory):
    executable=shutil.which('google-chrome-stable') or shutil.which('chromium')
    if not executable:
        pytest.skip('Chrome unavailable')
    folder=tmp_path_factory.mktemp('emoji-cdp')
    log=(folder/'chrome.log').open('w')
    process=subprocess.Popen([executable,'--headless=new','--no-sandbox','--disable-gpu',
        '--remote-debugging-port=0',f'--user-data-dir={folder}', 'about:blank'],stdout=log,stderr=log)
    try:
        deadline=time.monotonic()+15
        active=folder/'DevToolsActivePort'
        while not active.exists() and time.monotonic()<deadline and process.poll() is None:
            time.sleep(.05)
        assert active.exists(),(folder/'chrome.log').read_text()[-2000:]
        port,endpoint=active.read_text().splitlines()[:2]
        with connect(f'ws://127.0.0.1:{port}{endpoint}',open_timeout=5) as socket:
            yield Browser(socket)
    finally:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)
        log.close()


def document(width, context, packs):
    app=(ROOT/'static/js/client/app.js').read_text()
    placement=app[app.index('  function _placePop('):app.index('  // Keyboard navigation for the flat')]
    picker=app[app.index('  function openEmojiPopover('):app.index('  // Tapping the react button when')]
    buttons=app[app.index('  function _emojiBtn('):app.index('  // ---------- ":shortcode" autocomplete')]
    css=(ROOT/'static/css/client.css').read_text()
    icon='data:image/svg+xml;base64,'+base64.b64encode((ROOT/'static/vendor/cryptocurrency-icons/xmr.svg').read_bytes()).decode()
    names=['DRC_emojo']+[f'Pack {i:02}' for i in range(packs-2)]+['Monero-XMR']
    custom=[{'s':('xmr' if name=='Monero-XMR' else 'drc')+str(i),'p':name,'t':icon,'u':icon} for name in names for i in range(24)]
    script=f'''
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const enc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const REACTION_EMOJIS=Array(80).fill('👍'),InstEmoji={{list:{json.dumps(custom)},load:async()=>InstEmoji.list}};
const _emojiRecent=()=>['👍'],_emojiRemember=()=>{{}},_popKeys=()=>()=>{{}};
let picked=null, trusted=false;
document.addEventListener('mousedown',e=>{{if(e.target.closest('.ep-grid button'))trusted=e.isTrusted;}},true);
{placement}
{buttons}
{picker}
openEmojiPopover(document.querySelector('#react'),(emoji,close)=>{{picked=emoji;close();}},{{anchored:{str(context=='anchored').lower()}}});
'''
    return f'''<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{css}body{{zoom:1!important;display:block!important;margin:0}}#react{{position:fixed;right:18px;top:44%;}} </style>
<body><button id="react">React to message</button><script>{script}</script></body></html>'''


@pytest.mark.parametrize('width,height',[(320,568),(375,667),(1280,800)])
@pytest.mark.parametrize('context',['anchored','sheet'])
@pytest.mark.parametrize('packs',[2,12])
def test_pack_tabs_fit_and_select_real_xmr_images(chrome,tmp_path,width,height,context,packs):
    target=chrome.command('Target.createTarget',{'url':'about:blank'})['targetId']
    chrome.session=chrome.command('Target.attachToTarget',{'targetId':target,'flatten':True})['sessionId']
    try:
        chrome.command('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
        chrome.command('Emulation.setTouchEmulationEnabled',{'enabled':width<600})
        chrome.command('Page.enable')
        frame=chrome.command('Page.getFrameTree')['frameTree']['frame']['id']
        chrome.command('Page.setDocumentContent',{'frameId':frame,'html':document(width,context,packs)})
        chrome.evaluate('new Promise(resolve=>setTimeout(resolve,300))')
        layout=chrome.evaluate('''(()=>{const pop=document.querySelector('.emoji-pop'),tabs=pop.querySelector('.ep-tabs'),grid=pop.querySelector('.ep-grid'),r=pop.getBoundingClientRect();
          return {width:innerWidth,fits:r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1,
          wraps:getComputedStyle(tabs).flexWrap,scrollbar:getComputedStyle(tabs).scrollbarWidth,
          tabs:tabs.querySelectorAll('button').length,horizontal:tabs.scrollWidth>tabs.clientWidth+1,
          scrolls:tabs.scrollHeight>tabs.clientHeight+1,grid:grid.getBoundingClientRect().height};})()''')
        assert layout['width']==width,layout
        assert layout['fits'] and not layout['horizontal'] and layout['wraps']=='wrap',layout
        assert layout['tabs']==packs+2 and layout['grid']>=40,layout
        assert layout['scrollbar']!='none',layout
        if packs==12:
            assert layout['scrolls'],layout
            chrome.evaluate("document.querySelector('.ep-tabs').scrollTop=document.querySelector('.ep-tabs').scrollHeight")
        else:
            assert not layout['scrolls'],layout
        # Grow the initial one-item Recent tab through actual search input first.
        chrome.evaluate("(()=>{const q=document.querySelector('.ep-q');q.value='xmr';q.dispatchEvent(new Event('input',{bubbles:true}));})()")
        assert chrome.evaluate("document.querySelectorAll('.ep-grid img').length")==24
        assert chrome.evaluate("(()=>{const r=document.querySelector('.emoji-pop').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight+1})()"), 'Search growth moved the picker outside the viewport'
        chrome.evaluate("(()=>{const q=document.querySelector('.ep-q');q.value='';q.dispatchEvent(new Event('input',{bubbles:true}));})()")
        chrome.click('.ep-tab[data-tab="Monero-XMR"]',touch=width<600)
        chrome.evaluate('new Promise(resolve=>setTimeout(resolve,100))')
        assert chrome.evaluate("[...document.querySelectorAll('.ep-grid img')].every(i=>i.complete&&i.naturalWidth>0&&i.alt.startsWith(':xmr'))")
        assert chrome.evaluate("document.querySelectorAll('.ep-grid img').length")==24
        assert chrome.evaluate("(()=>{const r=document.querySelector('.emoji-pop').getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1})()"), 'Selecting a pack moved the picker outside the viewport'
        screenshot=chrome.command('Page.captureScreenshot',{'format':'png'})['data']
        (tmp_path/f'emoji-{width}-{context}-{packs}.png').write_bytes(base64.b64decode(screenshot))
        chrome.click('.ep-grid [data-e=":xmr0:"]',touch=width<600)
        assert chrome.evaluate('({picked,trusted,closed:!document.querySelector(".emoji-pop")})')=={'picked':':xmr0:','trusted':True,'closed':True}
    finally:
        chrome.session=None
        chrome.command('Target.closeTarget',{'targetId':target})
