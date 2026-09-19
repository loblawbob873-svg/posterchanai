/* The desktop Now-playing widget's equaliser dances ONLY while a track plays.
 *
 * Runs the SHIPPED os.js music-widget refresh(el) against a fake DOM + a stub player, and asserts
 * it adds `.playing` to `.wgt-music` when the player is playing and removes it when paused/stopped —
 * which is the single class that gates the CSS bar animation. A source check cannot see a refresh
 * that forgets the toggle; this can. */
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';

const src=fs.readFileSync(process.env.PC_OS_SOURCE||new URL('../../static/js/client/os.js',import.meta.url),'utf8');

// The music widget's refresh(el) — the first refresh(el){ after the 'Now playing' label.
const lab=src.indexOf("label: 'Now playing'"); assert.ok(lab>0,'music widget not found');
const rstart=src.indexOf('refresh(el){',lab); assert.ok(rstart>0,'music refresh not found');
// brace-match from the { of refresh(el){
let i=src.indexOf('{',rstart),depth=0,q='',esc=false,end=-1;
for(;i<src.length;i++){const c=src[i],n=src[i+1];
  if(q){if(esc)esc=false;else if(c==='\\')esc=true;else if(c===q)q='';continue;}
  if(c==='/'&&n==='/'){i=src.indexOf('\n',i);if(i<0)break;continue;}     // line comment
  if(c==='/'&&n==='*'){i=src.indexOf('*/',i+2)+1;continue;}               // block comment
  if(c==="'"||c==='"'||c==='`'){q=c;continue;}
  if(c==='{')depth++;else if(c==='}'){if(--depth===0){end=i;break;}}}
assert.ok(end>0,'could not brace-match refresh');
const body=src.slice(src.indexOf('{',rstart)+1,end);

function node(){const cls=new Set();return{classList:{add:c=>cls.add(c),remove:c=>cls.delete(c),
  toggle:(c,on)=>{if(on===undefined)on=!cls.has(c);on?cls.add(c):cls.delete(c);return on;},contains:c=>cls.has(c)},
  _cls:cls,textContent:'',style:{}};}
function fakeEl(){const n={};
  ['.wgt-mtitle','[data-m="toggle"]','.wgt-mnext','.wgt-mart','.wgt-mseekfill','.wgt-mt0','.wgt-mt1','[data-m="shuffle"]','.wgt-music'].forEach(s=>n[s]=node());
  return{querySelector:s=>n[s]||null,_n:n};}

function run(playing){
  const el=fakeEl();
  const player={now:()=>playing===null?null:({title:'Song',next:'Next',total:3,pos:1,playing:!!playing,d:100,t:5}),
    shuffling:()=>false};
  const ctx={console,Math,Number,$:(s,e)=>e.querySelector(s),PC:()=>({music:()=>player}),
    _mmss:x=>String(x|0),el};
  vm.createContext(ctx);
  vm.runInContext('(function(el){'+body+'})(el);',ctx);
  return el._n['.wgt-music']._cls.has('playing');
}

assert.equal(run(true),true,'a playing track must set .wgt-music.playing (bars dance)');
assert.equal(run(false),false,'a paused track must clear .playing (bars stop)');
assert.equal(run(null),false,'nothing playing → no .playing');
console.log('music widget eq: refresh toggles .playing with the player state');
