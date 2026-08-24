import fs from 'node:fs';
import vm from 'node:vm';

const data = new Map();
globalThis.localStorage = {
  getItem: key => data.has(key) ? data.get(key) : null,
  setItem: (key, value) => data.set(key, String(value)),
};

const classList = { add(){}, remove(){}, toggle(){} };
const feed = { innerHTML:'', classList };
const controls = new Map();
function control(id){
  if(!controls.has(id)) controls.set(id, { id, value:'', classList, focus(){}, click(){ this.onclick?.(); } });
  return controls.get(id);
}
const dollar = selector => selector === '#feed' ? feed : control(selector.slice(1));
const dollars = () => [];

globalThis.window = globalThis;
window.__PC = {
  $:dollar, $$:dollars, enc:s=>String(s), niceNip05:s=>s,
  viewer:()=>({npub:'npub1testidentity',profile:{display_name:'Test User'}}),
  toast:()=>{},
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
control('cc-create-go').click();
const rooms=JSON.parse(data.get('pc.concord.invites'));
if(rooms.length!==1 || !rooms[0].local || rooms[0].name!=='Runtime Test') throw new Error('create flow failed');

const input=control('cc-input');
input.value='hello concord';
let prevented=false;
input.onkeydown({key:'Enter',ctrlKey:false,metaKey:false,preventDefault(){prevented=true;}});
if(prevented || data.has('pc.concord.test.'+rooms[0].naddr)) throw new Error('plain Enter sent a message');
input.onkeydown({key:'Enter',ctrlKey:true,metaKey:false,preventDefault(){prevented=true;}});
const messages=JSON.parse(data.get('pc.concord.test.'+rooms[0].naddr));
if(!prevented || messages.length!==1 || messages[0].text!=='hello concord' || messages[0].by!=='Test User')
  throw new Error('Ctrl+Enter send flow failed');
console.log('concord runtime flow ok');
