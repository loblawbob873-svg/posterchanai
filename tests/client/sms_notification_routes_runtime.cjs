const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict'),path=require('path');
const root=process.argv[2],app=fs.readFileSync(path.join(root,'static/js/client/app.js'),'utf8');
const router=app.slice(app.indexOf('  function openOsNotificationRoute('),app.indexOf('  window.PCOpenNotificationRoute='));
const landingStart=app.indexOf('  async function routeFromPath(){');
const landing=app.slice(landingStart,app.indexOf('    const e = _entityFromPath();',landingStart))+'\n}';
const oswin=fs.readFileSync(path.join(root,'static/js/client/oswin.js'),'utf8');
const channels=[],pending=[];
class Channel {
 constructor(name){this.name=name;channels.push(this);}
 postMessage(data){for(const c of channels)if(c!==this&&c.name===this.name&&!c.closed)pending.push(()=>c.onmessage?.({data:JSON.parse(JSON.stringify(data))}));}
 close(){this.closed=true;}
}
function flush(){while(pending.length)pending.shift()();}
function page({native=false,url='https://fixture.invalid/client',shell=false}={}){
 const log={views:[],recipients:[],opened:[],other:[],focused:0};
 const loc=new URL(url);
 const ctx={console,URL,URLSearchParams,encodeURIComponent,decodeURIComponent,BroadcastChannel:Channel,
  location:loc,history:{state:null,replaceState(state,title,next){ctx.location=new URL(next,ctx.location);}},
  document:{querySelector:()=>({}),documentElement:{classList:{add(){}}}},localStorage:{getItem:()=>null},
  VIEW:'home',switchView:v=>{ctx.VIEW=v;log.views.push(v);},_withSms:fn=>fn({openNotification:({address})=>log.recipients.push(address)}),
  _withModule:(file,name,fn)=>fn({openNotification:value=>log.other.push(value)}),openThread:id=>log.other.push(id),
  _inWin:()=>ctx.PCOSWin.isWindow(),focus:()=>log.focused++,PCOS:{renderExtra:()=>false},
  open:(url)=>{log.opened.push(url);return ctx.openResult;},openResult:{},pcWM:shell?{}:null,
  PCOSShell:{available:()=>shell}};
 ctx.window=ctx;ctx.globalThis=ctx;ctx.__PC={switchView:ctx.switchView};
 vm.createContext(ctx);vm.runInContext(oswin,ctx);vm.runInContext(router+landing+';PCOpenNotificationRoute=openOsNotificationRoute;',ctx);
 return {ctx,log};
}
(async()=>{
 const browser=page();
 for(const address of ['+1 555 0100','sender@example.test','123:456']){
  assert.equal(browser.ctx.PCOpenNotificationRoute('texts:'+encodeURIComponent(address)),true);
  assert.equal(browser.log.views.at(-1),'texts');assert.equal(browser.log.recipients.at(-1),address);
 }
 for(const route of ['texts:','texts:%','texts:%00','texts:'+('x'.repeat(81))]){
  const before=browser.log.views.length;assert.equal(browser.ctx.PCOpenNotificationRoute(route),false);assert.equal(browser.log.views.length,before);
 }
 browser.ctx.PCOpenNotificationRoute('texts');assert.equal(browser.log.views.at(-1),'texts');
 browser.ctx.PCOpenNotificationRoute('calendar');assert.equal(browser.log.views.at(-1),'calendar');
 browser.ctx.PCOpenNotificationRoute('post:abc');assert.equal(browser.log.other.at(-1),'abc');
 browser.ctx.PCOpenNotificationRoute('concord:room:channel:message%3A42');assert.equal(browser.log.other.at(-1).message,'message:42');
 browser.ctx.PCOpenNotificationRoute('messages');assert.equal(browser.log.views.at(-1),'messages');
 browser.ctx.PCOpenNotificationRoute('unknown');assert.equal(browser.log.views.at(-1),'notifications');
 // Cold native open carries its recipient through startup rather than relying on an opener.
 const shell=page({shell:true});shell.ctx.PCOpenNotificationRoute('texts:%2B15550100');
 assert.equal(shell.log.views.length,0);assert.equal(shell.log.recipients.length,0);
 const url=new URL(shell.log.opened[0],shell.ctx.location);
 assert.equal(url.searchParams.get('pcwin'),'texts');assert.equal(url.searchParams.get('pcsms'),'+15550100');
 const child=page({native:true,url:url.href});await child.ctx.routeFromPath();
 assert.equal(child.ctx.__PC_SMS_OPEN_ADDRESS,'+15550100');assert.equal(child.log.views.at(-1),'texts');
 assert.equal(child.ctx.location.searchParams.has('pcsms'),false,'cold route consumed before reload');
 delete child.ctx.__PC_SMS_OPEN_ADDRESS;await child.ctx.routeFromPath();assert.equal(child.ctx.__PC_SMS_OPEN_ADDRESS,undefined);
 // Electron denies the second window creation after focusing the singleton (window.open => null).
 shell.ctx.openResult=null;shell.ctx.PCOpenNotificationRoute('texts:%2B15550200');flush();
 assert.equal(child.log.recipients.at(-1),'+15550200');assert.equal(shell.log.views.length,0);
 assert.ok(child.log.focused>0);assert.equal(shell.log.recipients.length,0);
 console.log('SMS notification routes: browser, cold native, reused native, validation and existing routes passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
