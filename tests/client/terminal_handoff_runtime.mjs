import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(process.argv[2]||new URL('../../static/js/client/term.js',import.meta.url),'utf8');
const document={querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},removeEventListener(){},
  visibilityState:'visible'};
function renderer(remembered=''){
  const memory=new Map(remembered?[['pc_tty_sid:test',remembered]]:[]);
  const window={__PC:{ $:()=>null,$$:()=>[],enc:x=>String(x),toast(){},authFetch(){},publish(){} },
    __PC_API_BASE__:'test',addEventListener(){},removeEventListener(){}};
  const context={window,document,location:{origin:'https://test.invalid'},sessionStorage:{
    getItem:k=>memory.get(k)||null,setItem:(k,v)=>memory.set(k,String(v)),removeItem:k=>memory.delete(k)},
    setTimeout:()=>0,clearTimeout(){},requestAnimationFrame:f=>f(),console,URL,WebSocket:function(){}};
  vm.createContext(context);vm.runInContext(source,context);
  return {term:window.PCTerm,memory};
}

const tabs=[
  {sid:'local:alpha',host:'local',label:''},
  {sid:'local:bravo',host:'local',label:''},
  {sid:'local:charlie',host:'local',label:''},
];
const moved={activeSid:'local:bravo',host:'local',label:'',tabs,
  scroll:{pinned:false,aboveBottom:37}};
const left=renderer('local:alpha');
if(!left.term.acceptHandoff(moved)) throw Error('source setup rejected');
const right=renderer('local:charlie');
if(!right.term.acceptHandoff(left.term.handoffState())) throw Error('destination rejected handoff');
let state=right.term.handoffState();
if(state.activeSid!=='local:bravo') throw Error('middle session identity changed: '+state.activeSid);
if(state.tabs.map(x=>x.sid).join(',')!==tabs.map(x=>x.sid).join(','))
  throw Error('tab order/index changed: '+JSON.stringify(state.tabs));
if(state.scroll.pinned!==false||state.scroll.aboveBottom!==37) throw Error('scroll choice changed');

/* Moving back is a second destination adoption, not an index lookup. A stale renderer may have
 * another active session remembered; the payload's stable session identity must win. */
if(!left.term.acceptHandoff(right.term.handoffState())) throw Error('return handoff rejected');
state=left.term.handoffState();
if(state.activeSid!=='local:bravo'||left.memory.get('pc_tty_sid:test')!=='local:bravo')
  throw Error('return handoff selected stale renderer tab');
if(new Set(state.tabs.map(x=>x.sid)).size!==3) throw Error('handoff duplicated a terminal tab');
console.log('terminal monitor handoff runtime: ok');
