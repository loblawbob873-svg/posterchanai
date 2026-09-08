from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_effects_return_runtime():
    run = subprocess.run(['node', str(ROOT/'tests/client/effects_return_runtime.mjs'),
                          str(ROOT/'static/js/client/app.js')], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr


def test_effects_has_a_persistent_return_control_and_clears_new_chat_context():
    source = (ROOT/'static/js/client/app.js').read_text()
    mount = source[source.index('  async function aiMount('):source.index('  async function aiLoadConversations(')]
    assert 'id="ai-back-social" hidden>← Back to Social</button>' in mount
    assert "$('#ai-back-social').onclick=()=>_returnFromEffect(_ai.fxReturn)" in mount
    # Run the actual early conversation-switch cleanup without requiring the network chat renderer.
    change = source[source.index('    if(_ai.fxReturn && _ai.fxReturn.ready'):source.index('    if(_ai.convId !== id){', source.index('  async function aiOpenConversation('))]
    script = "const assert=require('node:assert/strict');const _ai={fxReturn:{ready:true,conversation:12},replyTo:{id:'post'}};let updates=0;const _syncEffectReturn=()=>updates++;const id=13;"+change+"assert.equal(_ai.fxReturn,null);assert.equal(_ai.replyTo,null);assert.equal(updates,1);"
    run = subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=15)
    assert run.returncode == 0,run.stderr


def test_desktop_return_target_preserves_exact_frame_route_and_refuses_closed_frame():
    source = (ROOT/'static/js/client/os.js').read_text()
    helper = source[source.index('  function captureReturnTarget(){'):source.index('  function routeView(view, focusOnly){')]
    script = """
const assert=require('node:assert/strict');let on=true;
const original={body:{},view:'global',appView:'profile',appPath:'/original-profile',scroll:{top:480}},other={body:{},view:'ai'};
const wins=[original,other],feed={parentElement:original.body},$=()=>feed,calls=[];
const focusWin=(w,render)=>calls.push([w,render]);
"""+helper+"""
const back=captureReturnTarget();feed.parentElement=other.body;
assert.equal(back(),true);assert.equal(calls[0][0],original);assert.equal(calls[0][1],undefined);
assert.equal(original.appView,'profile');assert.equal(original.appPath,'/original-profile');assert.equal(original.scroll.top,480);
wins.splice(wins.indexOf(original),1);assert.equal(back(),false);assert.equal(calls.length,1);
on=false;assert.equal(captureReturnTarget(),null);
"""
    run=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=15)
    assert run.returncode==0,run.stderr


def test_effects_conversation_creation_does_not_override_a_new_selection():
    source = (ROOT/'static/js/client/app.js').read_text()
    helpers=source[source.index('  function _effectReturnValid('):source.index('  async function launchEffectStudio(')]
    start=source[source.index('  async function startEffectStudio('):source.index('  // Telegram-style Effects studio:')]
    new=source[source.index('  async function aiNewConversation('):source.index('  async function aiDeleteConversation(')]
    script="""
const assert=require('node:assert/strict');let resolvePost,resolveBlob,waitBlob=false,opened=[],attachments=[],guides=0;
let VIEW='ai',ME={pubkey:'alice'},_ai={convId:12,fxReturn:{owner:'alice',ready:false}};
const $=()=>null,toast=()=>{},window={},fetch=async url=>{
 if(url==='/api/conversations')return {json:()=>new Promise(r=>resolvePost=r)};
 return {ok:true,blob:()=>waitBlob?new Promise(r=>resolveBlob=r):Promise.resolve({type:'image/png'})};
};
const aiOpenConversation=async id=>{opened.push(id);_ai.convId=id};
const aiAddFiles=async files=>attachments.push(files),showEffectGuide=()=>guides++,File=class {};
"""+helpers+start+new+"""
(async()=>{
 const pending=startEffectStudio('https://media/source.png');await Promise.resolve();await Promise.resolve();
 _ai.convId=99;resolvePost({id:13});await pending;
 assert.equal(_ai.convId,99);assert.deepEqual(opened,[]);assert.deepEqual(attachments,[]);assert.equal(_ai.fxReturn,null);assert.equal(_ai.replyTo,null);
 _ai={convId:12,fxReturn:{owner:'alice',ready:false}};
 const wanted=startEffectStudio('https://media/source.png');await Promise.resolve();await Promise.resolve();
 resolvePost({id:14});await wanted;
 assert.deepEqual(opened,[14]);assert.equal(_ai.fxReturn.conversation,14);assert.equal(attachments.length,1);assert.equal(guides,1);
 _ai={convId:14,fxReturn:null};waitBlob=true;
 const screenshot=startEffectStudio('https://media/screenshot.png');await Promise.resolve();await Promise.resolve();
 resolvePost({id:15});await new Promise(r=>setImmediate(r));ME={pubkey:'bob'};resolveBlob({type:'image/png'});await screenshot;
 assert.equal(attachments.length,1,'old account screenshot must not attach after account change');
})().catch(e=>{console.error(e);process.exit(1)});
"""
    run=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=15)
    assert run.returncode==0,run.stderr
