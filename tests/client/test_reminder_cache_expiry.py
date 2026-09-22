"""Execute the shipped reminder cache across offline aging, history polls and account changes."""
from pathlib import Path
import subprocess

import pytest
from tests.client_source import client_source

ROOT = Path(__file__).resolve().parents[2]
BOOT = r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const cs=require(process.argv[2]),source=cs.clientSourceAt(process.argv[1]);
const block=source.slice(source.indexOf('  // Reminder history is not a Nostr event.'),source.indexOf('  function notifList(){'));
const alert=source.slice(source.indexOf('  function reminderAlert(text,data={})'),source.indexOf('  // Markdown + the backend',source.indexOf('  function reminderAlert(text,data={})')));
let now=Date.parse('2026-09-15T12:00:00Z');const day=86400000, storage=new Map();
class Clock extends Date {constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
const record=(id,age)=>({reminder_id:id,due_at:new Date(now-age*day).toISOString(),delivered_at:new Date(now-age*day).toISOString(),content:'appointment '+id,route:'calendar'});
const cached=(id,age)=>({type:'reminder',id:'reminder:'+id+':'+record(id,age).due_at,created_at:(now-age*day)/1000,content:'appointment '+id,route:'calendar'});
const key=(account='alice',base='https://one')=>'pc_reminder_history:'+base+':'+account;
function client(){
 const c={Date:Clock,Map,JSON,Number,String,Math,ME:{pubkey:'alice'},GUEST:false,_aiToken:'fixture',base:'https://one',
 _instanceBase:()=>c.base,_standalone:()=>false,ensureAiSession:async()=>{},
 localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 _fetchTimeout:async()=>{throw Error('offline');},bumpNotif(){},renderNotificationsSoon(){},loadNotifs(){},window:{},
 checks:0,notificationAllowed:()=>{c.checks++;return false;},osNotify(){throw Error('unexpected alert');},enc:String,LOGO:''};
 cs.installStateGlobals(c);vm.createContext(c);vm.runInContext(block+alert,c);return c;
}
const ids=c=>Array.from(c._reminderRows(),x=>x.id.split(':')[1]);
const reply=(items,history_days)=>({ok:true,json:async()=>({items,...(history_days===undefined?{}:{history_days})})});
'''

CASES = {
    'legacy_offline_cache': r'''
storage.set(key(),JSON.stringify([cached('old',75),cached('recent',1)]));
const c=client();assert.deepEqual(ids(c),['recent']);await c.hydrateReminderNotifications();
assert.deepEqual(ids(c),['recent'],'offline response resurrected expired legacy rows');
assert.deepEqual(ids(client()),['recent'],'reload resurrected expired rows');
''',
    'clock_advance': r'''
storage.set(key(),JSON.stringify([cached('near',6.99),cached('fresh',1)]));
const c=client();assert.deepEqual(ids(c),['near','fresh']);now+=day;
assert.deepEqual(ids(c),['fresh'],'already-open client retained expired cache');
now+=7*day;assert.deepEqual(ids(c),[]);
''',
    'offline_poll_repaints_expired_rows': r'''
storage.set(key(),JSON.stringify([cached('near',6.99)]));
const c=client();let painted=null;c.bumpNotif=()=>{painted=ids(c);};
now+=day;await c.hydrateReminderNotifications();
assert.deepEqual(painted,[],'failed network poll left expired notifications painted');
''',
    'stale_history_and_live': r'''
const c=client();c._fetchTimeout=async()=>reply([record('old',75),record('recent',1)]);
await c.hydrateReminderNotifications();assert.deepEqual(ids(c),['recent']);
c.reminderAlert('stale live calendar',record('old-live',75));
assert.equal(c.checks,0,'stale socket replay reached notification delivery');
c.reminderAlert('current reminder',record('live',0));
assert.equal(c.checks,1,'fresh positive control must reach notification preference');
assert.deepEqual(ids(c),['live','recent']);
''',
    'retention_owner_instance_and_reload': r'''
const c=client();c._fetchTimeout=async()=>reply([record('extended',20)],30);
await c.hydrateReminderNotifications();assert.deepEqual(ids(c),['extended']);
assert.deepEqual(ids(client()),['extended'],'server retention was not remembered offline');
storage.set(key('bob'),JSON.stringify([cached('bob-old',20)]));c.ME={pubkey:'bob'};
assert.deepEqual(ids(c),[],'Alice retention leaked to Bob');
storage.set(key('alice','https://two'),JSON.stringify([cached('other-node-old',20)]));c.ME={pubkey:'alice'};c.base='https://two';
assert.deepEqual(ids(c),[],'instance retention leaked to another backend');
''',
    'shorter_policy_prunes_existing_cache': r'''
storage.set(key(),JSON.stringify([cached('five-days',5),cached('today',.1)]));
const c=client();c._fetchTimeout=async()=>reply([],1);await c.hydrateReminderNotifications();
assert.deepEqual(ids(c),['today'],'successful empty response did not apply shorter retention');
assert.deepEqual(ids(client()),['today']);
''',
    'late_owner_response_cannot_change_retention': r'''
const c=client();let release;c._fetchTimeout=()=>new Promise(r=>release=r);
const pending=c.hydrateReminderNotifications();for(let i=0;i<10&&!release;i++)await Promise.resolve();assert.ok(release);
c.ME={pubkey:'bob'};release(reply([record('alice-old',20)],30));await pending;
storage.set(key('bob'),JSON.stringify([cached('bob-old',20)]));assert.deepEqual(ids(c),[]);
c.ME={pubkey:'alice'};storage.set(key(),JSON.stringify([cached('alice-old',20)]));assert.deepEqual(ids(c),[]);
''',
}

@pytest.mark.parametrize('case', CASES)
def test_reminder_cache_retention(case):
    script = BOOT + '\n(async()=>{\n' + CASES[case] + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
    result = subprocess.run(['node','-e',script,str(ROOT/'static/js/client/app.js'),str(ROOT/'tests/client/client_source.cjs')],
                            capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
