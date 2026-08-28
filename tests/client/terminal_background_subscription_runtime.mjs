import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(process.argv[2]||new URL('../../static/js/client/term.js',import.meta.url),'utf8');

function functionSource(name){
  const at=source.indexOf(`function ${name}(`);
  if(at<0)throw Error(`${name} missing`);
  const brace=source.indexOf('{',at);let depth=0,quote='',escape=false;
  for(let i=brace;i<source.length;i++){
    const c=source[i];
    if(quote){if(escape)escape=false;else if(c==='\\')escape=true;else if(c===quote)quote='';continue;}
    if(c==='"'||c==="'"||c==='`'){quote=c;continue;}
    if(c==='{')depth++;else if(c==='}'&&!--depth)return source.slice(at,i+1);
  }
  throw Error(`${name} is unterminated`);
}

const context={};vm.createContext(context);
vm.runInContext(`${functionSource('_makeLocalReplayGate')};this.make=_makeLocalReplayGate`,context);

let epoch=1,cursor=0,drawn='';
const deliver=ev=>{
  if(ev.t!=='out')return;
  if(typeof ev.seq==='number'&&ev.seq<=cursor)return;
  drawn+=ev.d;if(typeof ev.seq==='number')cursor=ev.seq;
};

/* Output arrives while the renderer is backgrounded, before backlog resolves. The snapshot owns
 * A+B and the queued push overlaps B: B must not be painted twice. */
let gate=context.make(()=>epoch===1,deliver);
gate.push({t:'out',d:'B',seq:2});
gate.finish({t:'out',d:'AB',seq:2});
if(drawn!=='AB')throw Error('overlapping backlog/push duplicated output: '+drawn);

/* A callback already queued from the old mount may run after focus/remount. It must be inert. */
epoch=2;
gate.push({t:'out',d:'STALE',seq:7});
const current=context.make(()=>epoch===2,deliver);
current.push({t:'out',d:'C',seq:3});
current.finish({t:'out',d:'',seq:2});
if(drawn!=='ABC')throw Error('focus cycle lost/duplicated output: '+drawn);

/* A caught-up snapshot has no bytes but its cursor is still authoritative. */
epoch=3;cursor=9;drawn='';
const caught=context.make(()=>epoch===3,deliver);
caught.push({t:'out',d:'OLD',seq:9});
caught.finish({t:'out',d:'',seq:9});
if(drawn!=='')throw Error('empty snapshot replayed a delayed callback: '+drawn);

/* Repeat background/focus cycles. Each snapshot overlaps one queued live event and every byte is
 * still drawn exactly once, in order. */
for(const [snapshot,push,seq] of [['ABCD','D',4],['ABCDE','E',5],['ABCDEF','F',6]]){
  epoch++;cursor=0;drawn='';
  const mine=epoch,g=context.make(()=>epoch===mine,deliver);
  g.push({t:'out',d:push,seq});g.finish({t:'out',d:snapshot,seq});
  if(drawn!==snapshot)throw Error(`cycle ${seq} rendered ${JSON.stringify(drawn)}`);
}
console.log('terminal background subscription runtime: ok');
