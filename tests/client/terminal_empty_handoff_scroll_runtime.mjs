import fs from 'node:fs';

const source=fs.readFileSync(process.argv[2]||new URL('../../static/js/client/term.js',import.meta.url),'utf8');
function fn(name){
  const start=source.indexOf(`function ${name}(`);if(start<0)throw Error(`${name} missing`);
  const brace=source.indexOf('{',start);let depth=0;
  for(let i=brace;i<source.length;i++){
    if(source[i]==='{')depth++;
    else if(source[i]==='}'&&!--depth)return source.slice(start,i+1);
  }
  throw Error(`${name} does not close`);
}

const timers=[];let cleared=[];
const build=new Function('setTimeout','clearTimeout','requestAnimationFrame',`
  let handoffScroll={pinned:false,aboveBottom:37},handoffRestoreT=null,followBottom=false;
  const calls=[];
  const term={buffer:{active:{baseY:120}},scrollToLine:n=>calls.push(n)};
  ${fn('_restoreHandoffScroll')}
  ${fn('_scheduleHandoffScroll')}
  return {schedule:_scheduleHandoffScroll,restore:_restoreHandoffScroll,calls,
    pending:()=>handoffScroll,timer:()=>handoffRestoreT};
`);
const api=build((cb)=>{timers.push(cb);return timers.length;},id=>cleared.push(id),cb=>cb());
api.schedule();
if(!api.timer())throw Error('READY fallback was not armed');
timers.shift()();
if(api.pending()!==null)throw Error('empty replay did not consume saved viewport');
if(api.calls.at(-1)!==83)throw Error('empty replay did not restore distance from bottom: '+api.calls);

/* Replay winning the race restores immediately and cancels READY's fallback. */
const timers2=[],cleared2=[];
const api2=build(cb=>{timers2.push(cb);return timers2.length;},id=>cleared2.push(id),cb=>cb());
api2.schedule();api2.restore();
if(!cleared2.length||api2.pending()!==null||api2.calls.at(-1)!==83)
  throw Error('replay did not replace the READY fallback cleanly');
console.log('terminal empty handoff viewport runtime: ok');
