"""Execute shipped history/preferences with controlled transport, never a public reminder."""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[2]

def test_reminder_history_reload_failed_hydration_owner_races_and_duplicates():
    js=r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const s=fs.readFileSync(process.argv[1],'utf8');
const block=s.slice(s.indexOf('  // Reminder history is not a Nostr event.'),s.indexOf('  function notifList(){'));
const alert=s.slice(s.indexOf('  function reminderAlert(text,data={})'),s.indexOf('  // Markdown + the backend',s.indexOf('  function reminderAlert(text,data={})')));
const storage=new Map(),row={reminder_id:7,due_at:'2026-09-08T12:00:00+00:00',delivered_at:'2026-09-08T12:00:01+00:00',content:'Calendar event',route:'calendar'};
function make(){
 const c={ME:{pubkey:'a'},GUEST:false,Date,Map,JSON,Number,String,_aiToken:'token',_instanceBase:()=>c.base,_standalone:()=>false,base:'https://one',
 localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},ensureAiSession:async()=>{},
 _fetchTimeout:async()=>({ok:true,json:async()=>({items:[row]})}),bumpNotif(){},renderNotificationsSoon(){},loadNotifs(){},
 window:{},notificationAllowed:()=>false,osNotify(){throw new Error('muted alert interrupted')},enc:String,LOGO:'',console};
 vm.createContext(c);vm.runInContext(block+alert,c);return c;
}
(async()=>{
 const a=make();a.reminderAlert(row.content,row);assert.equal(a._reminderRows().length,1,'muted history');a.reminderAlert(row.content,row);assert.equal(a._reminderRows().length,1,'dedup');
 const reload=make();assert.equal(reload._reminderRows().length,1,'reload');reload._fetchTimeout=async()=>{throw Error('offline')};await reload.hydrateReminderNotifications();assert.equal(reload._reminderRows().length,1,'failure preserves cache');
 vm.runInContext('_reminderLoads.clear()',reload);reload._fetchTimeout=async()=>({ok:true,json:async()=>({items:[row,{...row,reminder_id:8,content:'missed elsewhere'}]})});await reload.hydrateReminderNotifications();assert.equal(reload._reminderRows().length,2,'retry hydrates missed delivery');
 reload.ME={pubkey:'b'};assert.equal(reload._reminderRows().length,0,'account scope');reload.ME={pubkey:'a'};reload.base='https://two';assert.equal(reload._reminderRows().length,0,'instance scope');
 const race=make();let resolve;race.ensureAiSession=()=>new Promise(r=>resolve=r);let calls=0;race._fetchTimeout=async()=>{calls++;};const p=race.hydrateReminderNotifications();race.ME={pubkey:'b'};resolve();await p;assert.equal(calls,0,'owner changed during auth');
 const late=make();let release;late._fetchTimeout=()=>new Promise(r=>release=r);const q=late.hydrateReminderNotifications();for(let i=0;i<8&&!release;i++)await Promise.resolve();assert.ok(release);late.ME={pubkey:'b'};release({ok:true,json:async()=>({items:[row]})});await q;assert.equal(late._reminderRows().length,0,'late owner response');
 const raceHistory=make(),fresh={...row,reminder_id:99};raceHistory._rememberReminder(fresh);assert.equal(raceHistory._rememberReminder(fresh,undefined,true),true,'history before WS must not suppress first alert');assert.equal(raceHistory._rememberReminder(fresh,undefined,true),false,'delivered alert dedup');
 const repeat=make();repeat._rememberReminder({...row,due_at:'2026-09-09T12:00:00Z'});assert.equal(repeat._reminderRows().length,4,'new occurrence');for(let i=0;i<220;i++)repeat._rememberReminder({...row,reminder_id:100+i});assert.equal(repeat._reminderRows().length,200,'bounded');
 const conn=s.slice(s.indexOf('  function aiConnect(id){'),s.indexOf('  // WS upgrade failed',s.indexOf('  function aiConnect(id){')));
 const stale=make();stale._ai={};stale._serverOrigin=()=>stale.base;stale._cookie=()=>'';stale.setTimeout=()=>1;stale.clearTimeout=()=>{};stale.WebSocket=class{close(){}send(){throw Error('stale send')}};
 let delivered=0;stale.aiHandle=()=>delivered++;vm.runInContext(conn,stale);stale.aiConnect(4);const callback=stale._ai.ws.onmessage;
 callback({data:JSON.stringify(row)});assert.equal(delivered,1);stale.ME={pubkey:'b'};callback({data:JSON.stringify(row)});assert.equal(delivered,1,'old account socket cannot deliver');
 stale.ME={pubkey:'a'};stale.base='https://two';callback({data:JSON.stringify(row)});assert.equal(delivered,1,'old instance socket cannot deliver');
 console.log('history and account races passed');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result=subprocess.run(['node','-e',js,str(ROOT/'static/js/client/app.js')],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
