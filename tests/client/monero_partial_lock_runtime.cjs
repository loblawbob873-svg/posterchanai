'use strict';
/* A partly-confirming Monero balance is explained, not just shown as two numbers.
 * Reported: "why is so much of my balance always locked???? that last transaction was half an hour
 * ago". Runs the SHIPPED meWalletHtml from monero-wallet.js. */
const fs=require('fs'),path=require('path');
const acorn=require('/opt/flood/node_modules/acorn');
const src=fs.readFileSync(path.join(__dirname,'..','..','static','js','client','monero-wallet.js'),'utf8');
const ast=acorn.parse(src,{ecmaVersion:'latest',sourceType:'script',allowReturnOutsideFunction:true});
let fn=null; const consts=[];
(function walk(n){ if(!n||typeof n.type!=='string')return;
  if(n.type==='FunctionDeclaration'&&n.id&&n.id.name==='meWalletHtml'&&!fn)fn=src.slice(n.start,n.end);
  if(n.type==='VariableDeclaration'&&n.declarations.some(d=>d.id&&['esc','amount','xmr'].includes(d.id.name)))consts.push(src.slice(n.start,n.end));
  for(const k in n){const v=n[k];if(Array.isArray(v))v.forEach(walk);else if(v&&typeof v.type==='string')walk(v);} })(ast);
if(!fn) throw new Error('meWalletHtml is gone');
const render=new Function('atomicXmr','transferRows',consts.join('\n')+'\n'+fn+'\nreturn meWalletHtml;')(v=>String(v),()=>'');
const text=h=>h.replace(/<[^>]+>/g,' ').replace(/\s+/g,' ');
function ok(n,v){ if(!v) throw new Error(n); console.log('  ok   '+n); }
// The measured account: 0.847297 XMR total, 0.594876 of it received 16 blocks ago, 2 to go at the time.
let t=text(render({network:'mainnet',balance:'0.847297',unlocked_balance:'0.252421',blocks_to_unlock:2,address:'4x',transfers:[]}));
ok('the available figure is the spendable one', /Available balance 0\.252421 XMR/.test(t));
ok('it says how much is still confirming', /0\.594876 XMR confirming/.test(t));
ok('and roughly when it unlocks', /unlocks in about 4 min/.test(t) && /2 blocks to go/.test(t));
ok('and why', /10 confirmations/.test(t) && /change/.test(t));
t=text(render({network:'mainnet',balance:'0.8',unlocked_balance:'0.8',blocks_to_unlock:0,address:'4x',transfers:[]}));
ok('a fully spendable wallet says nothing about locking', !/confirming|to go/.test(t));
console.log('OK partial lock');
