"""A desktop's cached address book refreshes without opening Contacts."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_cached_names_refresh_and_old_account_reads_cannot_land():
    subprocess.run(['node', '-e', r'''
const assert=require('assert/strict'),fs=require('fs'),vm=require('vm');
let account='alice',now=1000,requests=0,fail=false,held=null;
const cards=name=>[{uid:'person',ics:JSON.stringify({uid:'person',fn:name,tels:[{value:'+15553334444'}]})}];
let serverCards=cards('From phone');
const storage=new Map([['pc_contacts_cache:alice',JSON.stringify({books:[{id:'main'}],cards:{main:cards('Cached name')}})]]);
const context={console,Map,Set,Promise,setTimeout,clearTimeout,
 Date:class extends Date{static now(){return now}},
 localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
 PCVcard:{parse:JSON.parse,sortKey:c=>c.fn},
 __PC:{VIEW:'texts',me:()=>({pubkey:account}),$(){return null},$$(){return []},
  enc:String,toast(){},modal(){},closeModal(){},ensureAiSession:async()=>{},capPlugin:()=>null,
  authFetch:async path=>{requests++;if(fail)throw Error('offline');
   if(path.endsWith('/books'))return {ok:true,json:async()=>({books:[{id:'main'}]})};
   if(held){const wait=held;held=null;return wait;}
   return {ok:true,json:async()=>({cards:serverCards})};}},
 PCSms:{refreshNames(){context.PCContacts.nameFor('5553334444')}},
};
context.window=context;vm.createContext(context);
vm.runInContext(fs.readFileSync('static/js/client/contacts.js','utf8'),context);
const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 assert.equal(context.PCContacts.nameFor('5553334444'),'Cached name');
 for(let i=0;i<50;i++)context.PCContacts.nameFor('5553334444');
 await settle();assert.equal(context.PCContacts.nameFor('5553334444'),'From phone');assert.equal(requests,2);
 serverCards=cards('Phone renamed');now+=30001;
 assert.equal(context.PCContacts.nameFor('5553334444'),'From phone');await settle();
 assert.equal(context.PCContacts.nameFor('5553334444'),'Phone renamed');assert.equal(requests,4);
 fail=true;now+=30001;context.PCContacts.nameFor('5553334444');await settle();
 assert.equal(context.PCContacts.nameFor('5553334444'),'Phone renamed');assert.equal(requests,5);
 fail=false;now+=30001;let release;
 held=new Promise(r=>release=r);context.PCContacts.nameFor('5553334444');await settle();
 account='bob';serverCards=cards('Bob contact');
 assert.equal(context.PCContacts.nameFor('5553334444'),'');await settle();
 assert.equal(context.PCContacts.nameFor('5553334444'),'Bob contact');
 release({ok:true,json:async()=>({cards:cards('Late Alice contact')})});await settle();
 assert.equal(context.PCContacts.nameFor('5553334444'),'Bob contact');
 assert.equal(JSON.parse(storage.get('pc_contacts_cache:bob')).cards.main[0].ics,JSON.stringify(JSON.parse(cards('Bob contact')[0].ics)));
 assert(!storage.get('pc_contacts_cache:bob').includes('Alice'));
 const beforeRender=requests;serverCards=cards('Updated on phone');now+=30001;
 context.PCContacts.render();await settle();
 assert.equal(requests,beforeRender+2);
 assert.equal(context.PCContacts.nameFor('5553334444'),'Updated on phone');
})().catch(e=>{console.error(e);process.exitCode=1});
'''], cwd=ROOT, check=True, timeout=20)
