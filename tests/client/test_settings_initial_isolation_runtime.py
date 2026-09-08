"""First-open Settings owns the client view before a late Social redraw can use its feed."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_first_settings_open_is_isolated_before_bridge_completion_and_late_social_redraw():
    source=(ROOT/'static/js/client/os.js').read_text()
    app=(ROOT/'static/js/client/app.js').read_text()
    opening=source[source.index('  let _openedReal = false;'):source.index("  let _osSettingsPage=")]
    renderer=source[source.index('  async function renderSystemSettings(){'):source.index('  function openTaskManager')]
    start=app.index('  function _drawTimeline(preserveScroll){')
    timeline=app[start:app.index('\n  function ',start+5)]
    script='''
const assert=require('node:assert/strict');let VIEW='global',queries=0,renders=0;
const host={parentElement:null,className:'',innerHTML:'Social',scrollTop:42};
const document={getElementById:()=>host},window={pcDisplays:{}};
const pcDisplays={status:()=>new Promise(()=>{})},_settingsRead=p=>p;
const wins=[],PC=()=>({adoptView:v=>{VIEW=String(v||'')}});let _osSettingsPage='displays';
function focusWin(w,render){
 host.parentElement=w.body;
 if(w.isolated)PC().adoptView(w.view);
 if(render!==false && w.render)w.render();
}
function openApp(view,label,icon,render,noFeed){
 const w={view,body:{},render,noFeed};wins.push(w);focusWin(w);return w;
}
const $=()=>host,_tlFilter=()=>()=>true,isMutedView=()=>false;
const Store={feed:()=>{queries++;throw Error('timeline queried Settings feed')}};
''' + opening + renderer + timeline + '''
const w=openSystemSettings();
assert(w.isolated && w.rerun,'Settings flags absent after opening');
assert.equal(host.innerHTML,'<div class="spinner"></div>','Settings bridge did not stay pending');
// A previously queued EOSE redraw runs while the first Settings display query has not answered.
try{_drawTimeline(false)}catch(e){if(e.message!=='timeline queried Settings feed')throw e;}
assert.equal(queries,0,'late Social timeline callback still targets Settings on first open');
assert.equal(VIEW,'doc:os-settings','first Settings render never adopted its own client view');
assert.equal(host.innerHTML,'<div class="spinner"></div>');
// Opening the existing Settings document after Social also adopts before rerendering.
VIEW='global';assert.equal(openSystemSettings(),w);assert.equal(VIEW,'doc:os-settings');
assert.equal(wins.length,1,'reopening Settings duplicated its window');
console.log('initial Settings isolation: ok');
'''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
