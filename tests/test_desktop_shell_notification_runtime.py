"""Exercise the shipped IPC handler, including authorization and standalone click routing."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def test_shell_does_not_activate_external_daemon_but_standalone_notifications_work():
    result=subprocess.run(['node','--input-type=module','-'],cwd=ROOT,input=r'''
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const text=fs.readFileSync('desktop/main.js','utf8');
const start=text.indexOf("ipcMain.handle('pc:host:notify'");
const code=text.slice(start,text.indexOf("ipcMain.handle('pc:host:pickDirectory'",start));
for(const shell of [true,false]) {
 const notes=[];let supported=0,handler,shown=0,focused=0;
 class Notification {
  static isSupported(){supported++;return true;}
  constructor(options){this.options=options;this.listeners={};notes.push(this);}
  on(name,fn){this.listeners[name]=fn;}
  show(){this.shown=true;}
 }
 const messages=[];const sender={isDestroyed:()=>false,send:(...args)=>messages.push(args)};
 const owner={show(){shown++;},focus(){focused++;}};
 const scope={SHELL_MODE:shell,ipcMain:{handle(name,fn){handler=fn;}},fsGuard(e){if(e.sender!==sender)throw Error('untrusted');},
  electron:{Notification},BrowserWindow:{fromWebContents:()=>owner},win:null,path:{join:()=>'/icon.png'},__dirname:'/'};
 vm.runInNewContext(code,scope);
 assert.throws(()=>handler({sender:{}},{}),/untrusted/,'authorization remains before suppression');
 for(let i=0;i<30;i++)assert.equal(handler({sender},{title:'New message',body:'body',route:'messages',silent:true}),true);
 if(shell){assert.equal(supported,0);assert.equal(notes.length,0,'no native daemon activation even under a burst');}
 else {
  assert.equal(notes.length,30);assert(notes.every(n=>n.shown&&n.options.silent));
  notes[0].listeners.click();assert.equal(shown,1);assert.equal(focused,1);
  assert.deepEqual(messages,[['pc:host:notification-click','messages']]);
 }
}
''',capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
