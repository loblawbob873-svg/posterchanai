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


def test_scaled_volume_nostr_and_notifications_align_with_their_actual_buttons():
    main = (ROOT / 'desktop/main.js').read_text()
    preload = (ROOT / 'desktop/preload.js').read_text()
    bridge = preload[preload.index('  const popupGeometry ='):
                     preload.index("  contextBridge.exposeInMainWorld('pcDisplays'")]
    opener = main[main.index('async function openPopupWindow('):
                  main.index('/* WAYLAND GIVES A CLIENT NO SAY')]
    placement = main[main.index('const _BAR_FLYOUTS = new Set('):
                     main.index("ipcMain.handle('pc:popup:close'")]
    script = r'''
const assert=require('node:assert/strict'),path=require('node:path');
const __dirname='.',APP_URL='https://example.invalid',POPUP_TITLE='PosterChan Popup';
const STICKY_POPUPS=new Set(['compose']),_shellScopes=new Map([[11,{output:'target'}]]);
const _workAreas=new Map();
let _popupWin,_popupKind,outputs,row,placed,clientSize,popupScale,bridgeAPI,invocation;
const window={innerWidth:3072,innerHeight:2048};
const contextBridge={exposeInMainWorld:(name,api)=>{bridgeAPI=api}};
const ipcRenderer={invoke:(...args)=>{invocation=args;return Promise.resolve(true)}};
const closePopupWindow=()=>{_popupWin=null};
const wm=()=>({outputs:async()=>outputs,focusedOutputName:async()=>'other',
 windows:async()=>[row],focus:async()=>{},placeAndReveal:async(id,x,y,w,h)=>{placed={x,y,w,h}}});
class BrowserWindow{
 constructor(opts){this.opts=opts;this.events={};
  row={id:88,title:POPUP_TITLE,rect:{x:0,y:0,width:opts.width*popupScale,height:opts.height*popupScale}};
  this.webContents={send:()=>{}};}
 on(name,fn){this.events[name]=fn} once(name,fn){this.events[name]=fn}
 isDestroyed(){return false} show(){} focus(){}
 getBounds(){return {width:this.opts.width,height:this.opts.height}}
 setSize(w,h){clientSize={w,h}}
 async loadURL(){this.events['ready-to-show']()}
}
''' + bridge + opener + placement + r'''
(async()=>{
 for(const [width,height,ox,oy] of [[3840,2560,0,0],[3840,2560,3840,0],
   [1280,800,-1280,0],[1080,1920,0,-1920]]){
  for(const scale of [1,1.25,1.5,2])for(popupScale of [1,1.5]){
   window.innerWidth=width/scale;window.innerHeight=height/scale;
   outputs=[{name:'target',rect:{x:ox,y:oy,width,height}},
            {name:'other',rect:{x:9999,y:0,width:1920,height:1080}}];
   const reserve=Math.round(48*scale);
   _workAreas.clear();_workAreas.set('target',{x:ox,y:oy,w:width,h:height-reserve,reserve});
   for(const [kind,w,liveRight] of [['tray',360,2867.9376],['net',420,2917.9375],['noti',430,2967.9375]]){
    for(const method of ['open','toggle']){
     const right=width===3840 && scale===1.25 ? liveRight : window.innerWidth-20;
     const h=Math.min(500,window.innerHeight-80),x=Math.round(right-w);
     await bridgeAPI[method](kind,{x,y:10,width:w,height:h,viewportWidth:1,viewportHeight:1});
     assert.equal(invocation[0],'pc:popup:'+method);
     assert.equal(invocation[2].viewportWidth,window.innerWidth,'metadata must be actual caller viewport');
     assert.equal(await openPopupWindow({sender:{id:11}},...invocation.slice(1)),true);
     await Promise.resolve();
     assert(Math.abs(placed.x+placed.w-(ox+right*scale))<=2,
       JSON.stringify({kind,scale,popupScale,right,placed}));
     assert(Math.abs(placed.w-w*scale)<=1,'size must use source scale, not initial popup monitor');
     assert(placed.y>=oy && placed.y+placed.h<=oy+height-reserve);
     assert(clientSize.w>=1 && clientSize.h>=1);
    }
   }
  }
 }
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
