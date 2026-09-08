"""Pointer ownership must not depend on another monitor's stale keyboard focus."""
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pointer_popups_use_clicked_surface_and_keyboard_ticks_have_one_owner():
    main = (ROOT / 'desktop/main.js').read_text()
    popup = main[main.index('async function openPopupWindow('):
                 main.index('/* WAYLAND GIVES A CLIENT NO SAY')]
    ticks = main[main.index('async function forwardShellTick('):
                 main.index('/* Proton commonly maps an anonymous XWayland')]
    script = r'''
const assert=require('node:assert/strict'),path=require('node:path');
const __dirname='.',APP_URL='https://example.invalid',POPUP_TITLE='PosterChan Popup';
const STICKY_POPUPS=new Set(['compose']),_shellScopes=new Map(),_shellSurfaces=new Map();
let _popupWin=null,_popupKind='',placed=null,created=0,focused='DP-1';
const outputs=[
 {name:'DP-1',rect:{x:-1920,y:0,width:1920,height:1080}},
 {name:'DP-2',rect:{x:0,y:-200,width:3840,height:2560}},
];
const wm=()=>({outputs:async()=>outputs,focusedOutputName:async()=>focused,
 workspaces:async()=>[{name:'shared',focused:true}]});
const closePopupWindow=()=>{_popupWin=null;_popupKind='';};
const placePopupWindow=(p,want)=>{placed=want;};
class BrowserWindow{
 constructor(opts){created++;this.opts=opts;this.events={};}
 on(name,callback){this.events[name]=callback;}
 once(name,callback){this.events[name]=callback;}
 isDestroyed(){return false;}
 show(){}
 getBounds(){return {width:this.opts.width,height:this.opts.height};}
 async loadURL(){this.events['ready-to-show']();}
}
const deliveries=[];
for(const [id,output] of [[11,'DP-1'],[22,'DP-2']]){
 const browser={isDestroyed:()=>false,isFocused:()=>false,
  webContents:{id,send:(channel,event)=>deliveries.push({id,payload:event.payload})}};
 _shellScopes.set(id,{output,workspace:'shared'});
 _shellSurfaces.set(output,{browser});
}
''' + popup + ticks + r'''
(async()=>{
 for(const kind of ['start','noti','net','tray'])for(const [sender,other,x,y] of [
   [22,'DP-1',12,-190],[11,'DP-2',-1908,10]]){
  focused=other;placed=null;
  const before=created;
  assert.equal(await openPopupWindow({sender:{id:sender}},kind,
    {x:12,y:10,width:430,height:560}),true,kind+' rejected the clicked surface');
  assert.equal(created,before+1);
  assert.deepEqual(placed,{x,y,w:430,h:560},kind+' opened on keyboard-focus output');
 }
 // A compositor shortcut is resolved once in the main process, even if both
 // surfaces share a workspace. Removing the pointer guard must not broadcast it.
 for(const [output,id] of [['DP-1',11],['DP-2',22]]){
  focused=output;deliveries.length=0;
  await forwardShellTick({change:'run',payload:'pc:start'});
  assert.deepEqual(deliveries,[{id,payload:'pc:start'}]);
 }
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
