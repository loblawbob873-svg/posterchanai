"""A Settings body click supersedes pending native focus even if its DOM class stayed focused."""
import json
import re
import subprocess
import tempfile
from html import unescape
from pathlib import Path
import pytest
from .test_notes_new_draft_runtime import CHROME, ROOT


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome is not installed')
def test_settings_body_cancels_delayed_social_focus_without_repainting_controls():
    source = (ROOT / 'static/js/client/os.js').read_text()
    handlers = source[source.index("    el.addEventListener('pointerdown', (e) => {"):
                      source.index('    /* Concord is a three-pane workspace.')]
    generation = source[source.index('  let _focusGeneration = 0;'):
                        source.index('  const _domCoveredNative = new Set();')]
    native = source[source.index('  function _focusNativeDecorated(id, token){'):
                    source.index('  function _focusNativeWhenShown(')]
    script = '''
const el=document.getElementById('settings'),input=el.querySelector('input'),w={native:null};
let _natFocusHold=false,focused=135,paints=0,activations=0,finishDecoration;
const _gesturePress=()=>false;
window.pcWM={decorate:()=>new Promise(resolve=>finishDecoration=resolve),focus:async id=>{focused=id;}};
const pcWM=window.pcWM;
const check=(ok,msg)=>{if(!ok)throw Error(msg)};
''' + generation + native + '''
function focusWin(win,render){
 check(win===w,'wrong window received focus');_claimFocus();focused=135;activations++;
 if(render!==false){paints++;el.innerHTML='<input value="repainted">';}
}
''' + handlers + '''
(async()=>{
 input.focus();input.setSelectionRange(2,5);
 let pending=_focusNativeDecorated(146,_claimFocus());
 // Settings retained its DOM focused class while the native taskbar action was pending.
 input.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,buttons:1}));
 finishDecoration(true);await pending;
 check(focused===135,'stale Social focus overrode later Settings body press');
 check(activations===1 && paints===0,'focused Settings was repainted instead of reactivated');
 check(input.isConnected && input.selectionStart===2 && input.selectionEnd===5,'input or selection lost');
 // The compatibility click following a pointerdown must not claim focus twice.
 input.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1}));
 check(activations===1,'one physical click requested focus twice');
 // Keyboard/assistive activation also cancels a native focus without needing pointerdown.
 pending=_focusNativeDecorated(146,_claimFocus());input.click();finishDecoration(true);await pending;
 check(focused===135 && activations===2 && paints===0,'keyboard activation failed to reclaim Settings');
 // A genuinely background DOM window retains its existing focus/render path.
 el.classList.remove('focused');
 input.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,buttons:1}));
 check(paints===1,'background-window focus behavior changed');
 document.getElementById('result').textContent=JSON.stringify({passed:true});
})().catch(e=>document.getElementById('result').textContent=JSON.stringify({error:e.message}));
'''
    with tempfile.TemporaryDirectory(prefix='pc-settings-focus-race-') as directory:
        page=Path(directory)/'test.html'
        page.write_text('<!doctype html><div id="settings" class="focused"><input value="editable settings"></div><pre id="result"></pre><script>'+script+'</script>')
        run=subprocess.run([CHROME,'--headless=new','--no-sandbox','--disable-gpu','--virtual-time-budget=1000','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr[-1000:]
    result=re.search(r'<pre id="result">(.*?)</pre>',run.stdout,re.S)
    assert result,run.stdout[-1000:]
    assert json.loads(unescape(result.group(1)))=={'passed':True}
