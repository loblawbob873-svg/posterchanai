import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const declaration = 'async function sendDm(pk, text){';
const start = source.indexOf(declaration);
if(start < 0) throw new Error('sendDm declaration missing');
let open = source.indexOf('{', start), depth = 0, end = -1;
for(let i=open; i<source.length; i++){
  if(source[i]==='{') depth++;
  else if(source[i]==='}' && --depth===0){ end=i+1; break; }
}
if(end < 0) throw new Error('sendDm extraction failed');

const calls={saved:0,ingested:0,published:[],remounted:0,inbox:0};
const context={
  signer:{nip17wrap:async(pk,text)=>({toPeer:{id:'peer',pk,text},toSelf:{id:'self',pk,text}})},
  Store:{saveEvent:()=>{calls.saved++;}},
  /* TRUE, because that is what a successful ingest answers — sendDm now uses the result to decide
     whether it must echo the message locally (see dm_echo_runtime.mjs). A stub returning undefined
     modelled a REFUSED ingest, which is a different test than this one. */
  ingestWrap:async()=>{calls.ingested++; return true;},
  /* Only reached when the ingest refuses; present so this sandbox cannot ReferenceError if the
     branch is ever taken. */
  _dmEcho:()=>{calls.echoed=(calls.echoed||0)+1; return true;},
  Relay:{publish:async ev=>{calls.published.push(ev.id);return {ok:true};},publishTo:async()=>1},
  VIEW:'messages', renderMessages:()=>{calls.remounted++;},
  _keepDmOpen:()=>{},
  dmInboxRelays:async()=>{calls.inbox++;return [];}, toast:()=>{},
  setTimeout, Promise,
};
vm.runInNewContext(source.slice(start,end)+'\nthis.run=sendDm;',context,{filename:'app-sendDm.js'});
await context.run('a'.repeat(64),'stay here');
await Promise.resolve();
if(calls.saved!==1 || calls.ingested!==1 || calls.published.join(',')!=='peer,self')
  throw new Error('successful NIP-17 send did not save, ingest and publish its two wraps');
if(calls.remounted!==0)
  throw new Error('successful DM send remounted Messages and discarded the open thread');
console.log('dm send stayed in the open thread');
