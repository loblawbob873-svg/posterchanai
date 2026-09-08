"""Replacing an unfinished local menu must not let its late load error close the replacement."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def test_popup_load_failure_only_closes_its_own_window():
    main=(ROOT/'desktop/main.js').read_text()
    opener=main[main.index('async function openPopupWindow('):main.index('/* WAYLAND GIVES A CLIENT NO SAY')]
    script=r'''
const assert=require('node:assert/strict'),path=require('node:path');
const APP_URL='app://posterchan/index.html',POPUP_TITLE='PosterChan Popup',STICKY_POPUPS=new Set(),_shellScopes=new Map();
let _popupWin=null,_popupKind='';const created=[];
const wm=()=>({outputs:async()=>[],focusedOutputName:async()=>''});
const placePopupWindow=()=>{},forwardShellTick=()=>{};
const closePopupWindow=()=>{const p=_popupWin;_popupWin=null;_popupKind='';if(p)p.destroy();};
class BrowserWindow{
 constructor(options){this.options=options;this.handlers={};this.dead=false;created.push(this)}
 on(event,fn){this.handlers[event]=fn}once(event,fn){this.on(event,fn)}
 isDestroyed(){return this.dead}getBounds(){return {width:420,height:560}}show(){}
 loadURL(url){this.url=url;return new Promise((resolve,reject)=>{this.resolve=resolve;this.reject=reject})}
 destroy(){this.dead=true;this.reject?.(Error('ERR_ABORTED'));this.handlers.closed?.()}
}
''' +opener+r'''
(async()=>{
 const first=openPopupWindow({sender:{id:1}},'start',{});
 await new Promise(setImmediate);
 assert.equal(created[0].url,APP_URL+'?pcpopup=start','menu document must be bundled and offline');
 const second=openPopupWindow({sender:{id:1}},'noti',{});
 await new Promise(setImmediate);
 assert.equal(await first,false,'replaced load should report cancellation');
 assert.equal(_popupWin,created[1],'old load failure closed the new popup');
 assert.equal(created[1].dead,false);
 created[1].resolve();assert.equal(await second,true);
 const third=openPopupWindow({sender:{id:1}},'tray',{});
 await new Promise(setImmediate);created[2].reject(Error('local resource failure'));
 assert.equal(await third,false);assert.equal(_popupWin,null,'current failed popup must close');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
