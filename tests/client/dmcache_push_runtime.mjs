/* Runs the SHIPPED DmCache.pushShared (dms.js) against a fake IndexedDB, relay and Blossom.
 * Measured on one account: ~400 copies of the ~1.5MB encrypted DM cache in two days, every one kept
 * forever — it re-uploaded when NOTHING had changed, from every PosterChanOS window, and never let
 * go of the copy it replaced. */
import fs from 'node:fs'; import vm from 'node:vm'; import path from 'node:path'; import {fileURLToPath} from 'node:url';
const HERE=path.dirname(fileURLToPath(import.meta.url));
const SRC=fs.readFileSync(path.join(HERE,'..','..','static','js','client','dms.js'),'utf8');
const store=new Map();
const localStorage={getItem:k=>store.has(k)?store.get(k):null,setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)};
const ME='4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6';
const log={uploads:0,published:[],deleted:[]};
let pointer={sha:'a'.repeat(64),n:5};
const key=await crypto.subtle.generateKey({name:'AES-GCM',length:256},false,['encrypt','decrypt']);
async function recs(n){const out=[];for(let i=0;i<n;i++){const iv=crypto.getRandomValues(new Uint8Array(12));
  const ct=await crypto.subtle.encrypt({name:'AES-GCM',iv},key,new TextEncoder().encode(JSON.stringify({kind:14,content:'m'+i})));
  out.push({k:ME.slice(0,16)+':w'+i,rec:{iv:[...iv],ct:[...new Uint8Array(ct)]}});}return out;}
function fakeDb(rows){return {transaction:()=>({objectStore:()=>({openCursor:()=>{const q={};let i=0;
  const step=()=>{q.result=i<rows.length?{key:rows[i].k,value:rows[i].rec,continue(){i++;setTimeout(step,0);}}:null;q.onsuccess&&q.onsuccess();};setTimeout(step,0);return q;}})})};}
function build(){
  const win={localStorage,crypto,TextEncoder,TextDecoder,setTimeout,clearTimeout,console,Promise,JSON,Date,Math,Uint8Array,Number,String,Object,Array,Map,Set,
    File:class{constructor(p,n,o){this.parts=p;this.name=n;this.type=o&&o.type;}},
    Relay:{query:async()=>[{created_at:1,content:JSON.stringify(pointer)}]},
    __PC:{deleteBlobQuiet:async sha=>{log.deleted.push(sha);return true;}}};
  win.window=win;
  vm.createContext(win); vm.runInContext(SRC,win);
  const dep={state:{get ME(){return {pubkey:ME};}},
    uploadBlob:async()=>{log.uploads++;return 'https://m.example/'+('b'+log.uploads).padEnd(64,'0');},
    _shaFromUrl:u=>String(u).split('/').pop(),
    publish:async(kind,content)=>{log.published.push(JSON.parse(content));pointer=JSON.parse(content);},
    mediaServer:()=>'https://m.example'};
  const mod=win.PCDmsFactory(new Proxy(dep,{get:(t,k)=>k in t?t[k]:(()=>{})}));
  return mod.DmCache;
}
function ok(n,v){ if(!v){ console.error('FAIL '+n, JSON.stringify(log)); process.exit(1);} console.log('  ok   '+n); }
async function push(held){ const c=build(); const rows=await recs(held); c._mk=async()=>key; c._idb=async()=>fakeDb(rows); c._dead=()=>false; return c.pushShared(); }

ok('nothing new → no upload', (await push(5))===false && log.uploads===0);
ok('more than published → one upload and one pointer', (await push(8))===true && log.uploads===1 && log.published.at(-1).n===8);
ok('the copy it replaced is let go of', log.deleted.length===1 && log.deleted[0]==='a'.repeat(64));
ok('another window a moment later with one more message does not upload again', (await push(9))===false && log.uploads===1);
console.log('OK dmcache push');
