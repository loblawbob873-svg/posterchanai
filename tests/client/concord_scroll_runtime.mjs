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
const listeners={};
const box={dataset:{},scrollTop:0,scrollHeight:100,clientHeight:40,isConnected:true,
  querySelector(){return content;},querySelectorAll(){return rows;},
  /* A LIST PER TYPE, because more than one thing listens now: the anchor's `remember` and the
     gesture watch both want `scroll`, and a stub that keeps only the last registration silently
     unsubscribes the first — which looks exactly like the feature being broken. */
  addEventListener(type,fn){(listeners[type]=listeners[type]||[]).push(fn);}};
const content={};
let rows=[];
let resizeCallback=null;
/* A FRAME IS NOT SYNCHRONOUS, AND PRETENDING IT IS HID A REAL BUG FOR THE LIFE OF THIS FILE.
 *
 * `setProgrammaticScroll` marks the scroller and clears the mark on the next frame, and `onscroll`
 * used to discard every scroll event while that mark was set. Run rAF inline and the mark is set and
 * cleared inside one call, so the window is zero-width and a reader's flick can never land in it —
 * the fixture agreed with the bug. Frames are deferred here, and `drain()` is what runs them.
 * `tests/client/test_concord_scroll_survives_a_restore.py` is the file that measures the window. */
const frames=[];
const drain=()=>{ while(frames.length) frames.splice(0).forEach(f=>f()); };
const context={
  window:{requestAnimationFrame:f=>{frames.push(f);return frames.length;}},
  Date:{now:()=>Date.now()},
  clearTimeout:()=>{},
  document:{querySelector:s=>s==='.cc-messages'?box:null},
  sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,String(v))},
  setTimeout:f=>{timers.push(f);return timers.length;},
  ResizeObserver:class{constructor(cb){resizeCallback=cb;}observe(){}disconnect(){}},
  scrollStates:new Map(),scrollKey:()=> 'room:general',
};
vm.createContext(context);
vm.runInContext([
  fn('function readScroll('),fn('function writeScroll('),fn('function setProgrammaticScroll('),
  fn('function repaintScrollTop('),fn('function preserveChatScroll('),
  fn('function enterChatBottom('),fn('function viewportAnchor('),
  fn('function programmaticScrollEvent('),'let chatGestureUntil=0;', fn('function chatHandOn('), fn('function scrollGesture('),
  fn('function watchPinnedRoomGrowth('),
  'globalThis.api={readScroll,writeScroll,preserveChatScroll,enterChatBottom,watchPinnedRoomGrowth};'
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

// Explicit entry follows delayed growth only until the reader takes control of the scroll.
box.scrollHeight=1000;
context.api.enterChatBottom();
timers.shift()();
const entered=context.api.readScroll('room:general');
entered.pinned=false;entered.top=240;box.scrollTop=240;
box.scrollHeight=1600;
while(timers.length)timers.shift()();
if(box.scrollTop!==240)throw Error('entry retries overrode a deliberate reader scroll');

// An image resolves later than every retry; ResizeObserver is the indefinite pin.
entered.pinned=true;
context.api.watchPinnedRoomGrowth(box);
box.scrollHeight=1400;resizeCallback();
if(box.scrollTop!==1400)throw Error('delayed media growth moved pinned room away from latest');

// Once the reader deliberately scrolls up, later media must not drag them down.
const state=context.api.readScroll('room:general');state.pinned=false;state.top=275;
rows=[{dataset:{messageId:'reading'},offsetTop:300,offsetHeight:40}];
box.scrollTop=275;(listeners.scroll||[]).forEach(f=>f({}));
// An attachment above the visible message hydrates and adds 240px.
rows[0].offsetTop=540;box.scrollHeight=1800;resizeCallback();
if(box.scrollTop!==515)throw Error('delayed media replaced the message being read');
// Growth below the anchor cannot move it.
box.scrollHeight=2000;resizeCallback();
if(box.scrollTop!==515)throw Error('media below the reader moved the viewport');

// Backfilled history is prepended. Preserve the visible message, not its obsolete pixel offset.
rows=[{dataset:{messageId:'visible'},offsetTop:300,offsetHeight:40}];
box.scrollTop=310;state.top=310;state.pinned=false;
context.api.preserveChatScroll(()=>{
  rows=[{dataset:{messageId:'older'},offsetTop:100,offsetHeight:40},
        {dataset:{messageId:'visible'},offsetTop:700,offsetHeight:40}];
  box.scrollHeight=2200;
});
drain();                       // preserveChatScroll restores on the NEXT frame, like the app does
if(box.scrollTop!==710)throw Error('prepended history replaced the message being read');

console.log('Concord delayed scroll behavior holds');
