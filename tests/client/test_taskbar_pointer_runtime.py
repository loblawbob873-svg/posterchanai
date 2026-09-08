"""Focus and hardware updates cannot replace a taskbar button during a physical click."""
import json
import re
import subprocess
import tempfile
from html import unescape
from pathlib import Path
import pytest
from .test_notes_new_draft_runtime import CHROME, ROOT


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome is not installed')
def test_taskbar_defers_focus_and_tray_redraw_until_click_finishes():
    source=(ROOT/'static/js/client/os.js').read_text()
    code=source[source.index('  let _barPointerHeld='):source.index('  function drawBar(){')]
    draw=source[source.index('  function drawBar(){'):source.index('  function drawBar(){')+180]
    assert 'if(!bar || _deferBarDraw()) return;' in draw
    assert "if(_deferBarDraw()) return;\n            try{\n              const shell =" in source
    watch=source[source.index('        PCOSShell.watch(() => {'):source.index('        }).then(off => {', source.index('        PCOSShell.watch(() => {'))]
    tick=watch[watch.index('          Promise.resolve(adoptAll())'):]
    code+='\nfunction hardwareTick(){return '+tick.strip()+'}\n'
    harness='''
const root=document.getElementById('root'),bar=document.getElementById('bar');let paints=0,clicks=0,trayPaints=0;
const $=(s,r)=>r.querySelector(s),adoptAll=async()=>false;
const PCOSShell={available:()=>true,paintTray:el=>{check(el.isConnected,'disconnected tray');trayPaints++;}};
function drawBar(){if(_deferBarDraw())return;paints++;bar.innerHTML='<button id="start">Start</button>';bar.firstChild.onclick=()=>clicks++;}
const check=(ok,msg)=>{if(!ok)throw Error(msg)},wait=()=>new Promise(r=>setTimeout(r,20));
(async()=>{
 drawBar();let button=bar.firstChild;
 button.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0}));
 drawBar();_deferBarDraw();drawBar();
 check(button.isConnected && paints===1,'focus or tray update detached pressed button');
 button.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,button:0}));
 check(button.isConnected,'pointerup rebuilt before click');
 button.click();check(clicks===1,'click action lost');
 await wait();check(paints===2,'queued updates not coalesced');
 button=bar.firstChild;button.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0}));
 drawBar();button.dispatchEvent(new PointerEvent('pointercancel',{bubbles:true}));
 await wait();check(paints===3,'cancel left taskbar frozen');
 drawBar();check(paints===4,'ordinary refresh was delayed');
 // The detached macOS tray belongs to this shell and has the same click protection.
 const tray=document.querySelector('.os-tray'),bell=tray.querySelector('button');
 const down=(el,id)=>el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:id}));
 const up=id=>document.body.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,button:0,pointerId:id}));
 down(bell,11);await hardwareTick();drawBar();
 check(paints===4 && trayPaints===0,'detached tray repaint during press');
 up(99);await wait();check(paints===4,'unrelated pointer released tray guard');
 down(bar.firstChild,12);up(11);await wait();
 check(paints===4,'first touch release cleared remaining touch');
 up(12);await wait();check(paints===5,'release outside taskbar did not drain redraw');
 await hardwareTick();check(trayPaints===1,'macOS detached tray did not refresh');
 // A newly started press keeps a queued redraw waiting until that press also completes.
 down(bar.firstChild,13);drawBar();up(13);down(bell,14);
 await wait();check(paints===5,'queued timer detached a newly pressed target');
 up(14);await wait();check(paints===6,'second press left updates frozen');
 down(bar.firstChild,15);drawBar();window.dispatchEvent(new Event('blur'));
 await wait();check(paints===7,'focus loss left updates frozen');
 // A tray outside the shell and ordinary keyboard activation do not acquire the guard.
 down(document.querySelector('#outside button'),16);drawBar();
 check(paints===8,'another widget blocked shell updates');
 bar.firstChild.click();drawBar();
 check(clicks===2 && paints===9,'keyboard-style activation blocked refresh');
 document.getElementById('result').textContent=JSON.stringify({passed:true});
})().catch(e=>document.getElementById('result').textContent=JSON.stringify({error:e.message}));
'''
    with tempfile.TemporaryDirectory(prefix='pc-taskbar-pointer-') as directory:
        page=Path(directory)/'test.html';page.write_text('<!doctype html><div id="root"><div id="bar"></div><div class="os-tray"><div id="os-shell"></div><button>Notifications</button></div></div><div class="os-tray" id="outside"><button>Other</button></div><pre id="result"></pre><script>'+code+harness+'</script>')
        run=subprocess.run([CHROME,'--headless=new','--no-sandbox','--disable-gpu','--virtual-time-budget=1000','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr[-1000:]
    result=re.search(r'<pre id="result">(.*?)</pre>',run.stdout,re.S)
    assert result,run.stdout[-1000:]
    assert json.loads(unescape(result.group(1)))=={'passed':True}
