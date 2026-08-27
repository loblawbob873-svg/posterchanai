import fs from 'node:fs';
import vm from 'node:vm';

const data=new Map();
globalThis.localStorage={getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,String(v))};
globalThis.sessionStorage={getItem:()=>null,setItem(){}};
const rooms=[
  {name:'Wrong',naddr:'wrong',local:true,channels:[{name:'general'}]},
  {name:'Target',communityId:'community:alpha',naddr:'target',local:true,
   channels:[{name:'general'},{name:'support',id:'support-id'}]},
];
data.set('pc.concord.invites',JSON.stringify(rooms));
data.set('pc.concord.test.target.support-id',JSON.stringify([{id:'message:42',by:'Ada',text:'target',at:1}]));
const row={dataset:{messageId:'message:42'},classList:{add(x){this.added=x;},remove(){}},isConnected:true,
  scrollIntoView(options){this.scrolled=options;}};
const classes={contains:()=>false,add(){},remove(){},toggle(){}};
const feed={innerHTML:'',insertAdjacentHTML(){}};
globalThis.document={body:{classList:classes},head:{appendChild(){}},documentElement:{appendChild(){}},
  createElement:()=>({dataset:{}}),querySelector:s=>s==='link[data-concord-css]'?{}:null,
  querySelectorAll:s=>s==='.cc-message[data-message-id]'?[row]:[],addEventListener(){}};
globalThis.window=globalThis; window.matchMedia=()=>({matches:false}); window.requestAnimationFrame=f=>f();
window.__PC={isView:v=>v==='concord',$:s=>s==='#feed'?feed:null,$$:()=>[],enc:String,viewer:()=>({}),
  profOf:()=>({}),linkify:String,linkCardHtml:()=>'',hydrateLinkCards(){},LOGO:''};
vm.runInThisContext(fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8'));

const route=PCConcord.notificationRoute(rooms[1],'support',{id:'message:42'});
if(route!=='concord:community%3Aalpha:support:message%3A42')throw new Error('notification lost exact identity: '+route);
if(!PCConcord.openNotification({community:'community:alpha',channel:'support',message:'message:42'}))
  throw new Error('saved target was not accepted');
const state=PCConcord.handoffState();
if(state.room!=='community:alpha'||state.channel!=='support')throw new Error('wrong room/channel selected');
if(!row.scrolled||row.scrolled.block!=='center'||row.classList.added!=='cc-message-target')
  throw new Error('target message was not revealed and highlighted');
if(PCConcord.openNotification({community:'missing',channel:'support',message:'message:42'}))
  throw new Error('missing community was redirected to another room');
console.log('concord notification deep-link runtime: ok');
process.exit(0);
