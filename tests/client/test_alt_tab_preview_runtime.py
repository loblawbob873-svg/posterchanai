"""A real DOM catches detached cached previews that permissive element mocks missed."""
import json
from html import unescape
from pathlib import Path
import re
import subprocess
import tempfile
import pytest
from .test_notes_new_draft_runtime import CHROME, ROOT


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome is not installed')
def test_switcher_raises_without_capture_wait_and_retains_connected_previews():
    src=(ROOT/'static/js/client/os.js').read_text()
    code=src[src.index('  let _altSwitch=null;'):src.index('  // ---- snapping',src.index('  let _altSwitch=null;'))]
    setup="""
const wins=[],nativeTasks=[{id:11,title:'Firefox',focused:true},{id:12,title:'Terminal'}];
const _natShell={id:99,app:'posterchan-desktop',fullscreen:false};
const toggleStart=()=>{},hideCtx=()=>{},enc=String,iconSvg=()=>'',appIcon=()=>'';
const _claimFocus=()=>1,_focusCompositorCurrent=()=>Promise.resolve(true),_focusNativeDecorated=()=>{};
let windowsCalls=0;const raised=[],captures=[],pending=new Map();
window.pcWM={windows:()=>{windowsCalls++;return Promise.resolve([_natShell]);},
 fullscreen:(id,on)=>{raised.push([id,on]);return Promise.resolve(true);},
 preview:id=>{captures.push(id);return new Promise(resolve=>pending.set(id,resolve));}};
const pcWM=window.pcWM;
"""
    exercise="""
(async()=>{
 const check=(ok,msg)=>{if(!ok)throw Error(msg)};
 cycleWindows('next');
 const overlay=document.querySelector('.os-alt-switch'), cards=[...overlay.children];
 check(cards.length===2,'first press draws immediately');
 await Promise.resolve();await Promise.resolve();await Promise.resolve();
 check(raised.some(x=>x[1]),'shell waits for screenshot capture');
 check(windowsCalls===0,'opening ignores cached shell identity');
 cycleWindows('next');
 check(overlay.children[0]===cards[0],'selection rebuilt cards while capture pending');
 check(captures.length===2,'selection started duplicate captures');
 for(const resolve of pending.values())resolve('data:image/png;base64,aGVsbG8=');
 await Promise.resolve();await Promise.resolve();
 check([...overlay.querySelectorAll('.os-alt-preview')].every(p=>p.style.backgroundImage),'initial previews missing');
 cycleWindows('next');
 check(overlay.children[0]===cards[0],'selection replaced connected preview');
 check([...overlay.querySelectorAll('.os-alt-preview')].every(p=>p.style.backgroundImage),'previews disappeared on Tab');
 nativeTasks.push({id:13,title:'New window'});cycleWindows('next');
 check([...overlay.querySelectorAll('.os-alt-preview')].slice(0,2).every(p=>p.style.backgroundImage),'cached previews applied while detached');
 _closeAltSwitch(false);pending.get(13)('data:image/png;base64,bGF0ZQ==');
 await Promise.resolve();await Promise.resolve();
 check(!document.querySelector('.os-alt-switch'),'late capture recreated closed switcher');
 check(raised.some(x=>!x[1]),'shell was not lowered');
 document.getElementById('result').textContent=JSON.stringify({passed:true});
})().catch(e=>document.getElementById('result').textContent=JSON.stringify({error:e.message}));
"""
    with tempfile.TemporaryDirectory(prefix='pc-alt-preview-') as td:
        page=Path(td)/'test.html';page.write_text('<!doctype html><pre id="result"></pre><script>'+setup+code+exercise+'</script>')
        result=subprocess.run([CHROME,'--headless=new','--no-sandbox','--disable-gpu','--virtual-time-budget=1000','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr[-1000:]
    match=re.search(r'<pre id="result">(.*?)</pre>',result.stdout,re.S)
    assert match,result.stdout[-1000:]
    assert json.loads(unescape(match.group(1)))=={'passed':True}
