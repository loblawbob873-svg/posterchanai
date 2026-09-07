"""Run the installed terminal selection/context-menu handlers with delayed native IPC."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_terminal_clipboard_runtime():
    source = Path(os.environ.get('PC_TERMINAL_JS', ROOT / 'static/js/client/term.js')).read_text()
    begin = source.index('      /* HIGHLIGHT COPIES,')
    code = source[begin:source.index('      term.onData', begin)]
    script = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const code=CODE;
function setup(){
 const pasted=[], written=[], notices=[], events={}; let select, selection='highlighted';
 const term={getSelection:()=>selection,onSelectionChange:fn=>select=fn,paste:s=>pasted.push(s)};
 const ctx={term,box:{isConnected:true,addEventListener:(n,fn)=>events[n]=fn},
 window:{pcClip:{write:async s=>{written.push(s);return true;}},pcClipRead:{read:async()=>written.at(-1)||'outside'}},
 navigator:{clipboard:{readText:async()=>''}},PC:{copyValue:async s=>written.push(s),toast:s=>notices.push(s)}};
 vm.runInNewContext(code,ctx);
 return {ctx,pasted,written,notices,select:s=>{selection=s;select();},paste:()=>events.contextmenu({preventDefault(){}})};
}
const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
(async()=>{
 // Native/Windows normal round trip preserves multiline Unicode and never stringifies objects.
 let t=setup();t.select('echo café\n日本語 🙂');await t.paste();assert.deepEqual(t.pasted,['echo café\n日本語 🙂']);
 for(const bad of [{text:'not a clipboard contract'},[{},{}],42,null,undefined]){
  t=setup();t.ctx.window.pcClipRead.read=async()=>bad;t.ctx.navigator.clipboard.readText=async()=>bad;
  await t.paste();assert.deepEqual(t.pasted,[]);assert.equal(t.notices.length,1);
 }
 // Invalid selection must not overwrite the system clipboard.
 t=setup();t.select({text:'bad'});await flush();assert.deepEqual(t.written,[]);
 // Native failure/invalid reply falls back to the browser's TEXT interface.
 for(const bad of [async()=>({}),async()=>{throw Error('denied');}]){
  t=setup();t.ctx.window.pcClipRead.read=bad;t.ctx.navigator.clipboard.readText=async()=> 'browser text';
  await t.paste();assert.deepEqual(t.pasted,['browser text']);
 }
 // Reading must wait for the pending native selection write, rather than paste stale clipboard data.
 t=setup();let finish;t.ctx.window.pcClip.write=s=>new Promise(r=>finish=()=>{t.written.push(s);r(true);});
 t.select('latest');const pending=t.paste();await flush();assert.deepEqual(t.pasted,[]);
 finish();await pending;assert.deepEqual(t.pasted,['latest']);
 // Rapid drag updates serialize so the final highlight wins even with delayed writes.
 t=setup();const completions=[];t.ctx.window.pcClip.write=s=>new Promise(r=>completions.push(()=>{t.written.push(s);r(true);}));
 t.select('first');t.select('last');const last=t.paste();await flush();assert.equal(completions.length,1);
 completions.shift()();await flush();assert.equal(completions.length,1);completions.shift()();await last;
 assert.deepEqual(t.pasted,['last']);
 // Rejected native copy is handled and gets a fallback, with no unhandled rejection.
 t=setup();t.ctx.window.pcClip.write=async()=>{throw Error('native copy failed');};
 t.select('fallback');await t.paste();assert.deepEqual(t.pasted,['fallback']);
 // Web-only clipboard remains supported, including exact whitespace.
 t=setup();t.ctx.window={};t.ctx.navigator.clipboard.readText=async()=> '  external\n';await t.paste();assert.deepEqual(t.pasted,['  external\n']);
 // A late clipboard read cannot target a closed or replacement terminal.
 for(const replace of [false,true]){
  t=setup();let done;t.ctx.window.pcClipRead.read=()=>new Promise(r=>done=r);
  const p=t.paste();await flush();if(replace)t.ctx.term={paste(){throw Error('wrong terminal');}};else t.ctx.box.isConnected=false;
  done('late');await p;assert.deepEqual(t.pasted,[]);
 }
 console.log('terminal clipboard runtime checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('CODE', __import__('json').dumps(code))
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
