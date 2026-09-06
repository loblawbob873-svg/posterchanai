import fs from 'node:fs';
import assert from 'node:assert/strict';
const src=fs.readFileSync(process.argv[2]||new URL('../../static/js/client/term.js',import.meta.url),'utf8');
const start=src.indexOf("          if(m.t === 'out'){"),end=src.indexOf("          if(m.t === 'ready'){",start);
assert(start>=0 && end>start);
function run(local){
 let drawn='',history='',state;
 state=new Function('term','link','onHistory',`
 let cursor=0,followBottom=true,scrollingByUs=false,handoffScroll=null;
 const _histSaw=onHistory,_restoreHandoffScroll=()=>{},_pinBottomAfterLayout=()=>{};
 return {frame:m=>{${src.slice(start,end)}},scrolling:()=>scrollingByUs};`)( {write:(text,cb)=>{assert.equal(state.scrolling(),true,'programmatic scrolling must be guarded before xterm writes');drawn+=text;cb?.();}}, {kind:local?'local':'ws'}, text=>history+=text );
 const frame=state.frame;
 const length=s=>local?s.length:new TextEncoder().encode(s).length;
 frame({t:'out',d:'hello🙂',seq:length('hello🙂')});
 frame({t:'out',d:'lo🙂 world',seq:length('hello🙂 world')});
 assert.equal(drawn,'hello🙂 world',`${local?'local':'SSH'} overlapping frame must contribute only its unseen suffix`);
 frame({t:'out',d:'lo🙂 world',seq:length('hello🙂 world')});
 assert.equal(drawn,'hello🙂 world','duplicate delivery must be ignored');
 assert.equal(history,drawn,'history must see exactly the newly rendered bytes, without replay duplicates');
}
run(true);run(false);
console.log('PASS: partial local/SSH replay overlap and UTF-8 are rendered once');
