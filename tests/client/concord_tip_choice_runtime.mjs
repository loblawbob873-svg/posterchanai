import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const body=source.split('function startConcordTip',2)[1].split('function invoiceModal',1)[0];
const calls=[];
const profiles=new Map([
  ['xmr-only',{name:'XMR member',monero_address:'4'.repeat(95)}],
  ['both',{name:'Both member',lud16:'member@example.com',monero_address:'8'.repeat(95)}],
  ['neither',{name:'No wallet'}],
]);
const context={
  profOf:pk=>profiles.get(pk),
  xmrOf:profile=>profile&&profile.monero_address,
  isXmrAddr:value=>typeof value==='string'&&value.length===95,
  doXmrTip:(note,pk)=>calls.push(['xmr',note,pk]),
  toast:value=>calls.push(['toast',value]),
  _lightningAmountSheet:(profile,callback)=>calls.push(['lightning',profile.name,callback]),
  _tipMethodSheet:(profile,methods,go)=>calls.push(['sheet',profile.name,methods,go]),
};
const start=vm.runInNewContext('(function startConcordTip'+body+')',context);

start('xmr-only',()=>{});
let sheet=calls.shift();
if(sheet[0]!=='sheet'||sheet[2].length!==1||sheet[2][0][0]!=='xmr')
  throw new Error('an XMR-only Concord member did not get the Social Monero choice');
sheet[3]('xmr');
if(JSON.stringify(calls.shift())!==JSON.stringify(['xmr',null,'xmr-only']))
  throw new Error('the Concord Monero choice did not enter Social\'s wallet flow');

const lightningCallback=()=>{};
start('both',lightningCallback);
sheet=calls.shift();
if(sheet[2].map(choice=>choice[0]).join(',')!=='ln,xmr')
  throw new Error('a dual-rail Concord member did not get Lightning and Monero choices');
sheet[3]('ln');
const lightning=calls.shift();
if(lightning[0]!=='lightning'||lightning[1]!=='Both member'||lightning[2]!==lightningCallback)
  throw new Error('the Lightning choice did not preserve Concord\'s sealed-zap callback');

start('neither',()=>{});
sheet=calls.shift();sheet[3]('ln');
if(calls.shift()?.[0]!=='toast')throw new Error('a member with no payment rail opened a broken zap flow');
