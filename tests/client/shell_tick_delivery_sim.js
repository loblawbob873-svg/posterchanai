'use strict';
/* HOW MANY TIMES DOES ONE COMPOSITOR KEY PRESS REACH THE PAGE?
 *
 * sway can only run a command, so every desktop binding is `swaymsg -t send_tick <payload>` and the
 * shell forwards it to the renderer. There are two forwarders in main.js — `wireShellRecovery`'s
 * (the authoritative one) and the per-renderer `pc:wm:subscribe` loop — and for a long time BOTH
 * registered a `tick` listener on the same socket. Measured on the real machine with a second
 * `pcWM.onEvent` listener installed through the debugger: `{"pc:probe-one":2,"pc:probe-two":2}`.
 *
 * A doubled tick never looks like a doubled tick. It looks like Alt+Tab skipping a window and
 * leaping to the other monitor on the first press, like Super doing nothing (opened, then closed),
 * like two terminals, like two screenshots. So this counts, against the shipped file.
 */
const fs=require('fs'), path=require('path');
const main=fs.readFileSync(process.env.PC_INSTALLED_MAIN_JS||
  path.resolve(__dirname,'../../desktop/main.js'),'utf8');

function ok(n,v){ if(!v) throw new Error(n); console.log('  ok   '+n); }

/* A tiny compositor: one socket, listeners by event name, exactly as WM.subscribe/on behave. */
const handlers={};
const wmObj={
  available:()=>true,
  subscribe:async()=>true,
  on:(name,fn)=>{(handlers[name]=handlers[name]||[]).push(fn);},
  workspaces:async()=>[{name:'ws-left',focused:true}],
  outputs:async()=>[{name:'DP-1',focused:true},{name:'DP-2',focused:false}],
  windows:async()=>[],
};

const delivered=[];
const webContents={id:1,send:(ch,ev)=>{if(ch==='pc:wm:event'&&ev&&ev.name==='tick')delivered.push(ev.payload);}};
const browserWindow={webContents,isDestroyed:()=>false};

/* Both forwarders, lifted verbatim from the shipped file. */
const fwdStart=main.indexOf('async function forwardShellTick');
const fwdEnd=main.indexOf('async function wireShellRecovery');
if(fwdStart<0||fwdEnd<0)throw new Error('forwardShellTick not found');
const forwardSrc=main.slice(fwdStart,fwdEnd);

const subStart=main.indexOf("  const NAMES = ['window', 'workspace', 'output', 'tick'];");
const subEnd=main.indexOf('  return true;\n});',subStart);
if(subStart<0||subEnd<0)throw new Error('pc:wm:subscribe registration loop not found');
const subSrc=main.slice(subStart,subEnd);

const scope={workspace:'ws-left',output:'DP-1'};
const _shellScopes=new Map([[1,scope]]);
const _shellSurfaces=new Map();
const SHELL_MODE=true;
const wm=()=>wmObj;
const BrowserWindow={getAllWindows:()=>[browserWindow]};
const scheduleDisplayReconcile=()=>{};
const _nativeOwners=new Map();
const require_=()=>({flatten:()=>[]});

const ctx={handlers,wm,BrowserWindow,_shellScopes,_shellSurfaces,SHELL_MODE,
  scheduleDisplayReconcile,_nativeOwners,console,setTimeout,clearTimeout,Promise,
  process:{pid:1},require:require_};

const body='(async()=>{\n'+forwardSrc+'\n'+
  'globalThis.__pcTestForwardShellTick=forwardShellTick;\n'+
  'const w=wm();\n'+subSrc+'\n'+
  '/* wireShellRecovery\'s one listener, which is the authoritative keyboard path. */\n'+
  'w.on("tick",(ev)=>{ if(!ev||ev.first)return; if(ev.payload!=="pc:restart") forwardShellTick(ev).catch(()=>{}); });\n'+
  '})()';
const fn=new Function(...Object.keys(ctx), body);
fn(...Object.values(ctx));

(async()=>{
  await new Promise(r=>setTimeout(r,0));
  ok('a tick listener is installed at all',(handlers.tick||[]).length>0);
  for(const f of handlers.tick) f({change:'run',payload:'pc:cycle:next'});
  await new Promise(r=>setTimeout(r,30));
  ok('one compositor tick reaches the page exactly once, not twice',
     delivered.filter(p=>p==='pc:cycle:next').length===1);

  delivered.length=0;
  for(const f of handlers.tick) f({change:'run',payload:'pc:start'});
  await new Promise(r=>setTimeout(r,30));
  ok('and so does the Super key',delivered.length===1);

  /* The focused-output filter still applies — a binding belongs to the screen you pressed it on. */
  delivered.length=0;
  _shellScopes.set(1,{workspace:'ws-right',output:'DP-2'});
  for(const f of handlers.tick) f({change:'run',payload:'pc:start'});
  await new Promise(r=>setTimeout(r,30));
  ok('a tick is not delivered to an unfocused output',delivered.length===0);

  /* Two shell renderers can share a workspace name. Every actionable route must still have one
   * focused-output owner; testing the route matrix catches a central dispatcher regression instead
   * of blessing Drafts while Texts or an external browser link still duplicates. */
  const routed=[];
  const left={webContents:{id:1,send:(ch,ev)=>{if(ch==='pc:wm:event')routed.push([1,ev.payload]);}},isDestroyed:()=>false};
  const right={webContents:{id:2,send:(ch,ev)=>{if(ch==='pc:wm:event')routed.push([2,ev.payload]);}},isDestroyed:()=>false};
  BrowserWindow.getAllWindows=()=>[left,right];
  _shellScopes.set(1,{workspace:'shared',output:'DP-1'});
  _shellScopes.set(2,{workspace:'shared',output:'DP-2'});
  wmObj.workspaces=async()=>[{name:'shared',focused:true}];
  const routes=['pc:terminal','pc:open:global','pc:open:texts','pc:open:concord','pc:open:monero',
    'pc:open:office','pc:open:drafts','pc:open:settings','pc:open:firefox',
    'pc:act:app:app%3Afirefox-bin','pc:act:app:app%3Asteam','pc:act:app:app%3Alibreoffice-writer',
    'pc:act:app:app%3Aorg.example.Generic',
    'pc:act:view:social','pc:act:profile:alice','pc:act:thread:event',
    'pc:act:find:https%3A%2F%2Fexample.com'];
  for(const payload of routes){
    routed.length=0;
    await globalThis.__pcTestForwardShellTick({change:'run',payload});
    ok(payload+' is claimed by exactly one shell',routed.length===1&&routed[0][0]===1);
  }

  console.log('OK one press is one tick');
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
