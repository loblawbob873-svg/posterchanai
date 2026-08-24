import fs from 'node:fs';
import vm from 'node:vm';

const data = new Map();
globalThis.localStorage = {
  getItem: key => data.has(key) ? data.get(key) : null,
  setItem: (key, value) => data.set(key, String(value)),
};

const makeClassList = () => ({added:[],removed:[],add(...x){this.added.push(...x);},remove(...x){this.removed.push(...x);},toggle(){}});
const classList = makeClassList();
const feed = { innerHTML:'', classList, insertAdjacentHTML(){} };
const controls = new Map();
function control(id){
  if(!controls.has(id)) controls.set(id, { id, value:'', classList:makeClassList(), focus(){}, click(){ this.onclick?.(); } });
  return controls.get(id);
}
const dollar = selector => selector === '#feed' ? feed : control(selector.slice(1));
const dollars = () => [];
const calls = {toasts:[], notified:0, group:0};

globalThis.window = globalThis;
window.__PC = {
  $:dollar, $$:dollars, enc:s=>String(s), niceNip05:s=>s,
  viewer:()=>({npub:'npub1testidentity',profile:{display_name:'Test User'}}),
  toast:s=>calls.toasts.push(String(s)),
  openEmojiPopover:(_anchor,pick)=>pick('😀',()=>{}),
  insertAt:(input,value)=>{ input.value+=value; },
  uploadBlob:async file=>'https://files.example/'+file.name,
  askOsNotify:async()=>{ calls.notified++; return 'granted'; },
  startGroupCall:()=>{ calls.group++; },
  profOf:()=>({}), LOGO:'', linkify:s=>String(s), linkCardHtml:()=>'', hydrateLinkCards:()=>{},
};
globalThis.document = {
  body:{classList}, head:{appendChild(){}}, documentElement:{appendChild(){}},
  createElement:()=>({dataset:{}}),
  querySelector:s=>s==='link[data-concord-css]'?{}:null,
  addEventListener(){},
};

vm.runInThisContext(fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8'),
                    {filename:'concord.js'});
PCConcord.render();

control('cc-community-name').value='Runtime Test';
control('cc-community-icon').value='🚀';
control('cc-create-go').click();
const rooms=JSON.parse(data.get('pc.concord.invites'));
if(rooms.length!==1 || !rooms[0].local || !rooms[0].channels || rooms[0].channels[0].private!==false || rooms[0].name!=='Runtime Test' || rooms[0].icon!=='🚀') throw new Error('create flow failed');

control('cc-edit-icon').click();
control('cc-icon-value').value='https://example.test/room.png';
control('cc-icon-save').click();
const edited=JSON.parse(data.get('pc.concord.invites'));
if(edited[0].icon!=='https://example.test/room.png') throw new Error('icon edit flow failed');
control('cc-description-value').value='Editable room description';
control('cc-settings-icon').value='🌌';
control('cc-settings-save').click();
const configured=JSON.parse(data.get('pc.concord.invites'));
if(configured[0].description!=='Editable room description' || configured[0].icon!=='🌌') throw new Error('community settings flow failed');

const input=control('cc-input');
control('cc-emoji').click();
if(input.value!=='😀') throw new Error('emoji control failed');
input.value='';
const file=control('cc-file'); file.files=[{name:'photo.png',size:1000}];
await file.onchange();
if(input.value!=='https://files.example/photo.png') throw new Error('attachment control failed');
input.value='';
control('cc-members').click();
if(!control('cc-members-dialog').classList.removed.includes('hidden')) throw new Error('members control failed');
await control('cc-notify').onclick();
if(calls.notified!==1) throw new Error('notification control failed');
control('cc-call').click();
if(!calls.toasts.some(x=>x.includes('No other community members'))) throw new Error('empty call state failed');
input.value='hello concord';
let prevented=false;
input.onkeydown({key:'Enter',ctrlKey:false,metaKey:false,preventDefault(){prevented=true;}});
if(prevented || data.has('pc.concord.test.'+rooms[0].naddr)) throw new Error('plain Enter sent a message');
input.onkeydown({key:'Enter',ctrlKey:true,metaKey:false,preventDefault(){prevented=true;}});
const messages=JSON.parse(data.get('pc.concord.test.'+rooms[0].naddr));
if(!prevented || messages.length!==1 || messages[0].text!=='hello concord' || messages[0].by!=='Test User')
  throw new Error('Ctrl+Enter send flow failed');
console.log('concord runtime flow ok');
