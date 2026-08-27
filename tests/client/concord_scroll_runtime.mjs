/* Execute the shipped Concord scroll helpers against delayed history/media growth. */
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
function fn(head){
  const i=source.indexOf(head),begin=source.indexOf('{',i);if(i<0||begin<0)throw Error('missing '+head);
  let depth=0;
  for(let p=begin;p<source.length;p++){
    if(source[p]==='{')depth++;
    else if(source[p]==='}'&&--depth===0)return source.slice(i,p+1);
  }
  throw Error('unterminated '+head);
}

const timers=[];
const storage=new Map();
const box={dataset:{},scrollTop:0,scrollHeight:100,clientHeight:40,isConnected:true,
  querySelector(){return content;}};
const content={};
let resizeCallback=null;
const context={
  window:{requestAnimationFrame:f=>f()},
  document:{querySelector:s=>s==='.cc-messages'?box:null},
  sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,String(v))},
  setTimeout:f=>{timers.push(f);return timers.length;},
  ResizeObserver:class{constructor(cb){resizeCallback=cb;}observe(){}disconnect(){}},
  scrollStates:new Map(),scrollKey:()=> 'room:general',
};
vm.createContext(context);
vm.runInContext([
  fn('function readScroll('),fn('function writeScroll('),fn('function setProgrammaticScroll('),
  fn('function enterChatBottom('),fn('function watchPinnedRoomGrowth('),
  'globalThis.api={readScroll,writeScroll,enterChatBottom,watchPinnedRoomGrowth};'
].join('\n'),context);

// Entry initially reaches the small placeholder history.
context.api.enterChatBottom();
while(timers.length)timers.shift()();
if(box.scrollTop!==100)throw Error('room entry did not reach initial bottom');

// History arrives after entry. The last retry must follow it.
box.scrollHeight=900;
context.api.enterChatBottom();
while(timers.length)timers.shift()();
if(box.scrollTop!==900)throw Error('delayed history moved entry away from latest');

// An image resolves later than every retry; ResizeObserver is the indefinite pin.
context.api.watchPinnedRoomGrowth(box);
box.scrollHeight=1400;resizeCallback();
if(box.scrollTop!==1400)throw Error('delayed media growth moved pinned room away from latest');

// Once the reader deliberately scrolls up, later media must not drag them down.
const state=context.api.readScroll('room:general');state.pinned=false;state.top=275;
box.scrollTop=275;box.scrollHeight=1800;resizeCallback();
if(box.scrollTop!==275)throw Error('delayed media destroyed deliberate scroll position');

console.log('Concord delayed scroll behavior holds');
