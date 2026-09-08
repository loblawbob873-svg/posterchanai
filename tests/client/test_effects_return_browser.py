"""Real DOM sizing and delayed painter scroll restoration for the Effects return control."""
import json
import re
import subprocess
import tempfile
from html import unescape
from pathlib import Path
import pytest
from .test_notes_new_draft_runtime import CHROME, ROOT


@pytest.mark.skipif(not Path(CHROME).exists(),reason='Chrome is not installed')
@pytest.mark.parametrize('source_view',['thread','profile'])
def test_effects_return_control_and_delayed_thread_scroll(source_view):
    src=(ROOT/'static/js/client/app.js').read_text()
    helpers=src[src.index('  function _effectReturnValid('):src.index('  async function launchEffectStudio(')]
    scroll=src[src.index('  function _putScroll('):src.index('  function _rememberTlScroll(')]
    restore=src[src.index('  function _restoreNavScroll('):src.index('  // "Has a person done anything yet?"')]
    bar=re.search(r'<div class="ai-bar">.*?</div>',src[src.index('  async function aiMount('):]).group(0)
    setup='''
let ME={pubkey:'alice'},VIEW='ai',_routing=false,_scrollRestoreGen=0;
const _TL_TABS=['home','global','trending'],_tlScrollMemo={},$=s=>document.querySelector(s),toast=()=>{};
let _ai={},resolvePaint;
const NT=()=>({nip19:{decode:()=>({type:'nevent',data:{id:'original-thread'}})}});
const openThread=()=>{VIEW='thread';feed.innerHTML='<div style="height:1200px">Cached head</div>';return new Promise(r=>resolvePaint=()=>{feed.innerHTML='<div style="height:1600px">Final thread</div>';feed.scrollTop=0;r()})};
const check=(v,m)=>{if(!v)throw Error(m)},pause=()=>new Promise(r=>setTimeout(r,30));
const feed=document.getElementById('feed');
function target(){return {owner:'alice',view:'thread',url:location.href,entity:{q:'original'},scroll:{pcv:'thread',top:180},ready:true,conversation:1};}
'''
    if source_view=='profile':
        setup=setup.replace("'thread'","'profile'").replace("type:'nevent',data:{id:'original-thread'}","type:'nprofile',data:{pubkey:'original-profile'}")
        setup+='\nconst renderProfileView=openThread;\n'
    exercise='''
(async()=>{try{
 const context=target();_ai={fxReturn:context,convId:1,replyTo:{id:'post'}};
 _syncEffectReturn();const button=document.getElementById('ai-back-social');button.onclick=()=>_returnFromEffect(_ai.fxReturn);
 check(!button.hidden && button.getBoundingClientRect().width>60,'Back button invisible');
 const bar=document.querySelector('.ai-bar');check(bar.scrollWidth<=bar.clientWidth+1,'mobile toolbar overflow');
 button.click();check(feed.scrollTop===180,'cached head did not restore');resolvePaint();await pause();
 check(feed.scrollTop===180,'final async thread paint lost original scroll');
 VIEW='ai';_ai={fxReturn:target(),convId:1,replyTo:{id:'post'}};_returnFromEffect(_ai.fxReturn);
 feed.dispatchEvent(new Event('wheel'));resolvePaint();feed.scrollTop=75;await pause();
 check(feed.scrollTop===75,'late completion overrode real user input');
 VIEW='ai';_ai={fxReturn:target(),convId:1};_returnFromEffect(_ai.fxReturn);
 ME={pubkey:'bob'};resolvePaint();feed.scrollTop=35;await pause();check(feed.scrollTop===35,'late completion crossed accounts');
 document.getElementById('result').textContent=JSON.stringify({passed:true});
}catch(e){document.getElementById('result').textContent=JSON.stringify({error:e.stack})}})();
'''
    css=(ROOT/'static/css/client.css').read_text()
    html='<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>'+css+'</style>'
    html+='<style>body{display:block!important;margin:0!important}.ai-chat{width:360px;max-width:100%}#feed{height:300px;overflow:auto;overflow-anchor:none;width:360px}#result{white-space:pre-wrap}</style>'
    html+='<div class="ai-chat">'+bar+'</div><div id="feed"></div><pre id="result"></pre><script>'+setup+helpers+scroll+restore+exercise+'</script>'
    with tempfile.TemporaryDirectory(prefix='pc-effects-return-browser-') as temp:
        path=Path(temp)/'index.html';path.write_text(html)
        run=subprocess.run([CHROME,'--headless=new','--no-sandbox','--disable-gpu','--window-size=390,844','--virtual-time-budget=1500','--dump-dom',path.as_uri()],capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr[-1000:]
    match=re.search(r'<pre id="result">(.*?)</pre>',run.stdout,re.S);assert match,run.stdout[-1000:]
    assert json.loads(unescape(match.group(1)))=={'passed':True}
