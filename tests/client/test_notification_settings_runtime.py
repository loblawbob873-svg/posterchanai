"""Execute shipped account notification persistence and delivery gates, with transport boundaries."""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[2]


def test_notification_preferences_account_reload_delayed_sync_and_multi_device():
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const block=source.slice(source.indexOf('  // Account-scoped alert preferences.'),source.indexOf('  // END ACCOUNT NOTIFICATION PREFERENCES'));
const ownerA='a'.repeat(64),ownerB='b'.repeat(64),server=new Map([[ownerA,{vimKeys:true,notificationPrefs:{email:true,dm:true}}]]);
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};}
function device(storage=new Map()){
 const c={ME:{pubkey:ownerA},ClientSettings:{get:(k,d)=>d},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
  window:{addEventListener(){}},$(){return null},$$(){return[]},enc:String,Date,setTimeout,clearTimeout,Promise,console,
  _prefsSaveChain:Promise.resolve(),read:null,signature:null,accepted:true,publishes:[],events:[],
  _readPrefs:async owner=>c.read?c.read.promise:structuredClone(server.get(owner)||{}),
  sign:async(kind,content,tags,created_at)=>{const event={kind,content,tags,created_at,pubkey:c.ME.pubkey};if(c.signature)await c.signature.promise;return event;},
  Relay:{publish:async event=>{c.publishes.push(event);if(c.accepted)server.set(event.pubkey,JSON.parse(event.content));return{ok:c.accepted};}},
  Store:{saveEvent:event=>c.events.push(event)}};
 vm.createContext(c);vm.runInContext(block+source.slice(source.indexOf('  function saveClientPrefsNostr(patch){'),source.indexOf('  async function restoreClientPrefsNostr')),c);c.publish=async(kind,content,tags,opts)=>c.Relay.publish(await c.sign(kind,content,tags,opts.createdAt));c.storage=storage;return c;
}
(async()=>{
 const a=device();assert.equal(a.notificationPreference('email'),true);
 await a._syncNotificationPreferences();assert.equal(a.notificationPreference('dm'),true);
 a.read=deferred();const change=a.setNotificationPreference('email',false);
 assert.equal(a.notificationAllowed('email'),false,'apply immediately');
 a._hydrateNotificationPreferences(ownerA,{email:true,dm:false});
 assert.equal(a.notificationPreference('email'),false,'late hydration cannot erase edit');
 assert.equal(a.notificationPreference('dm'),false,'untouched remote field hydrates');
 const reloaded=device(a.storage);assert.equal(reloaded.notificationPreference('email'),false,'reload pending value');
 a.read.resolve(structuredClone(server.get(ownerA)));await change;a.read=null;
 assert.equal(server.get(ownerA).vimKeys,true,'preserve unrelated preferences');
 assert.equal(server.get(ownerA).notificationPrefs.email,false);
 const b=device();await b._syncNotificationPreferences();assert.equal(b.notificationPreference('email'),false,'new device restores');
 await b.setNotificationPreference('dm',false);await a._syncNotificationPreferences();assert.equal(a.notificationPreference('dm'),false,'multi-device change');
 await a.setNotificationPreference('sound','off');a.ClientSettings.get=()=>false;
 a.ME={pubkey:ownerB};assert.equal(a.notificationPreference('sound'),'chime','fresh account ignores legacy device mute');assert.equal(a.notificationPreference('email'),true,'account must not inherit A');
 a._hydrateNotificationPreferences(ownerA,{email:false});assert.equal(a.notificationPreference('email'),true,'old account restore ignored');
 const race=device();race.read=deferred();const pending=race.setNotificationPreference('likes',false);await Promise.resolve();race.ME={pubkey:ownerB};race.read.resolve(server.get(ownerA));await pending;
 assert.equal(race.publishes.length,0,'account switched while reading: no signing/publish');
 race.ME={pubkey:ownerA};assert.equal(race.notificationPreference('likes'),false,'old owner pending survives');
 const signing=device();signing.signature=deferred();const saving=signing.setNotificationPreference('quotes',false);
 for(let i=0;i<5;i++)await Promise.resolve();signing.ME={pubkey:ownerB};signing.signature.resolve();await saving;
 assert.equal(signing.publishes.length,0,'account switched during signer wait: never publish');
 const failed=device();failed.accepted=false;await failed.setNotificationPreference('reposts',false);
 assert.equal(Object.keys(failed._notificationState().dirty).length,1,'negative ACK retains pending');
 const retry=device(failed.storage);await retry._syncNotificationPreferences();assert.equal(Object.keys(retry._notificationState().dirty).length,0,'retry clears only after ACK');
 const offline=device();offline._readPrefs=async()=>null;await offline.setNotificationPreference('zaps',false);assert.equal(offline.publishes.length,0,'unanswered read cannot replace document');
 assert.equal(offline.notificationPreference('zaps'),false);
 for(const sound of ['chime','soft','bright','off']){await b.setNotificationPreference('sound',sound);assert.equal(b.notificationPreference('sound'),sound);}
 await b.setNotificationPreference('sound','https://invalid');assert.equal(b.notificationPreference('sound'),'off');
 await b.saveClientPrefsNostr({vimKeys:false});assert.equal(server.get(ownerA).notificationPrefs.sound,'off','later generic save keeps notifications');assert.equal(server.get(ownerA).vimKeys,false);
 const stamps=b.publishes.map(e=>e.created_at);assert.ok(stamps.every((ts,i)=>!i||ts>stamps[i-1]),'rapid choices use strictly increasing replaceable timestamp');
 const before=b.notificationPreference('sound');b._hydrateNotificationPreferences(ownerA,undefined);assert.equal(b.notificationPreference('sound'),before,'empty relay response cannot clear saved choices');b._hydrateNotificationPreferences(ownerA,{sound:'bright'},1);assert.equal(b.notificationPreference('sound'),before,'older relay response cannot revert confirmed choice');
 console.log('account, delayed hydration, reload, ACK, signer race, multi-device and sound validation passed');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result=subprocess.run(['node','-e',script,str(ROOT/'static/js/client/app.js')],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr


def test_event_toggles_gate_actual_delivery_without_gating_other_events():
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const ping=source.slice(source.indexOf('  function notifPing(ev){'),source.indexOf('  // `html` is trusted markup'));
const notify=source.slice(source.indexOf('  function osNotify(title, body, opts){'),source.indexOf('  function osNotify(title, body, opts){')+9000);
let depth=0,end=0;for(let i=notify.indexOf('{');i<notify.length;i++){if(notify[i]==='{')depth++;if(notify[i]==='}'&&!--depth){end=i+1;break;}}
const type=source.slice(source.indexOf('  function _notificationType(opts){'),source.indexOf('  let _notificationRefreshAt'));
const disabled=new Set(),toasts=[],native=[];
const c={notificationAllowed:k=>!disabled.has(k),notificationSound(){},window:{pcHost:{notify:o=>native.push(o)}},
 pcHost:{notify:o=>native.push(o)},_SHORTCODE_STRIP:/:fixture:/g,LOGO:'icon',_capPlugin:()=>null,openOsNotificationRoute(){},
 _tipNote:()=>null,zapSender:e=>e.pubkey,isMutedAuthor:()=>false,profOf:()=>({name:'sender'}),
 fmtSats:String,zapAmount:()=>1,reactDisp:()=>'+',_quotesMe:e=>!!e.quote,isReply:e=>!!e.reply,
 emojiName:(pk,n)=>n,enc:String,_notifCtxId:()=>'',notifToast:(...args)=>toasts.push(args),openThread(){},switchView(){}};
vm.createContext(c);vm.runInContext(type+notify.slice(0,end)+ping,c);
for(const [kind,key,extra] of [[7,'likes',{}],[6,'reposts',{}],[9735,'zaps',{}],[1111,'replies',{}],[1,'quotes',{quote:true}],[1,'mentions',{}]]){
 disabled.add(key);const before=native.length,nt=toasts.length;c.notifPing({kind,pubkey:'peer',id:'id',...extra});assert.equal(native.length,before,key);assert.equal(toasts.length,nt,key);
 disabled.delete(key);c.notifPing({kind,pubkey:'peer',id:'id',...extra});assert.equal(native.length,before+1,key+' enabled');
}
for(const [key,opts] of [['email',{tag:'pc-mail',route:'mail'}],['dm',{tag:'pc-dm'}],['concord',{route:'concord:room:channel:message'}],['reminders',{tag:'pc-reminder'}]]){
 disabled.add(key);const count=native.length;c.osNotify('title','body',opts);assert.equal(native.length,count,key+' off');
 c.osNotify('title','body',{tag:'unrelated'});assert.equal(native.length,count+1,'unrelated still delivered');
 disabled.delete(key);c.osNotify('title','body',opts);assert.equal(native.length,count+2,key+' on');assert.equal(native.at(-1).silent,true,'native sound follows app sound owner');
}
'''
    result=subprocess.run(['node','-e',script,str(ROOT/'static/js/client/app.js')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_native_desktop_silent_flag_keeps_permission_guard_and_click_route():
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8'),start=source.indexOf("ipcMain.handle('pc:host:notify',");
const block=source.slice(start,source.indexOf("ipcMain.handle('pc:host:pickDirectory'",start));
const notes=[];let handler,guards=0,shown=0,focused=0,route;
class Notification{static isSupported(){return true;}constructor(options){this.options=options;notes.push(this);}on(name,fn){this.click=fn;}show(){shown++;}}
const owner={show(){},focus(){focused++;}},sender={isDestroyed:()=>false,send:(name,value)=>route=value};
vm.runInNewContext(block,{SHELL_MODE:false,ipcMain:{handle:(name,fn)=>handler=fn},fsGuard:e=>{guards++;if(e.denied)throw Error('denied');},electron:{Notification},BrowserWindow:{fromWebContents:()=>owner},win:owner,path:{join:()=>'/icon'},__dirname:'/app'});
for(const value of [true,false,'true',undefined]){handler({sender},{title:'hello',body:'body',silent:value,route:'mail'});assert.equal(notes.at(-1).options.silent,value===true);}
assert.equal(guards,4);assert.equal(shown,4);notes[0].click();assert.equal(focused,1);assert.equal(route,'mail');
assert.throws(()=>handler({denied:true,sender},{silent:true}));assert.equal(shown,4);
'''
    result=subprocess.run(['node','-e',script,str(ROOT/'desktop/main.js')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_notification_sound_mute_deduplication_and_audio_context_cleanup():
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const block=source.slice(source.indexOf('  let _notificationLastSound='),source.indexOf('  function _notificationType('));
let selected='chime',clock=10000;const contexts=[],timers=[];
class Audio {constructor(){this.currentTime=0;this.state='running';this.frequencies=[];this.closed=false;contexts.push(this)}createGain(){return{connect(){},gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}}}}createOscillator(){const o={type:'',frequency:{value:0},connect(){},start:()=>this.frequencies.push(o.frequency.value),stop(){}};return o}close(){this.closed=true}}
const c={window:{AudioContext:Audio},Date:{now:()=>clock},notificationPreference:()=>selected,setTimeout:f=>timers.push(f)};
vm.createContext(c);vm.runInContext(block,c);
c.notificationSound();c.notificationSound();assert.equal(contexts.length,1,'toast and OS notification chime once');
assert.deepEqual(contexts[0].frequencies,[523.25,783.99]);timers.splice(0).forEach(f=>f());assert.equal(contexts[0].closed,true,'no persistent desktop idle inhibitor');
selected='off';c.notificationSound(true);assert.equal(contexts.length,1,'silent preview creates no audio context');
selected='soft';c.notificationSound(true);assert.deepEqual(contexts.at(-1).frequencies,[392,523.25]);
selected='bright';c.notificationSound(true);assert.deepEqual(contexts.at(-1).frequencies,[659.25,987.77]);
c.window.Capacitor={};clock+=1000;const count=contexts.length;c.notificationSound();assert.equal(contexts.length,count,'no duplicate Android channel chime');
'''
    result=subprocess.run(['node','-e',script,str(ROOT/'static/js/client/app.js')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
