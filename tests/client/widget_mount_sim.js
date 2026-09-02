/* MOUNT EVERY DESKTOP WIDGET AND SEE WHAT IT DRAWS.
 *
 * Fourteen widgets shipped with two tests between them. A widget that throws on mount, or mounts
 * and draws nothing, fails in the quietest possible way: the tile is empty, nothing is logged, and
 * the desktop simply looks broken. Reported as "really double check the widgets" / "the widgets are
 * in a shit shape now".
 *
 * This runs the SHIPPED os.js in a stub DOM and, for each widget in the exported registry, calls
 * mount() and then refresh() and records what came out. It deliberately does NOT stub the network:
 * a widget must draw SOMETHING before its data arrives — a loading state is a pass, a blank tile is
 * not, because a blank tile is what a person sees when a fetch never resolves.
 */
const fs=require('fs'), path=require('path'), vm=require('vm');
const ROOT=path.resolve(__dirname,'../..');
const src=fs.readFileSync(process.env.PC_OS_JS||path.join(ROOT,'static/js/client/os.js'),'utf8');

function el(tag){
  const node={ tagName:(tag||'div').toUpperCase(), innerHTML:'', textContent:'', value:'',
    className:'', id:'', title:'', hidden:false, style:{ setProperty(){}, removeProperty(){} },
    dataset:{}, children:[], attributes:{},
    classList:{ _s:new Set(), add(...c){c.forEach(x=>this._s.add(x));}, remove(...c){c.forEach(x=>this._s.delete(x));},
                toggle(c,on){ on===undefined? (this._s.has(c)?this._s.delete(c):this._s.add(c)) : (on?this._s.add(c):this._s.delete(c)); },
                contains(c){return this._s.has(c);} },
    appendChild(c){ this.children.push(c); return c; }, append(...c){ this.children.push(...c); },
    insertAdjacentHTML(){}, insertAdjacentElement(){}, removeChild(){}, remove(){},
    setAttribute(k,v){ this.attributes[k]=v; }, getAttribute(k){ return this.attributes[k]??null; },
    removeAttribute(k){ delete this.attributes[k]; }, hasAttribute(k){ return k in this.attributes; },
    addEventListener(){}, removeEventListener(){}, focus(){}, blur(){}, click(){},
    querySelector(){ return el(); }, querySelectorAll(){ return []; }, closest(){ return null; },
    getBoundingClientRect(){ return {width:320,height:180,top:0,left:0,right:320,bottom:180}; },
    scrollIntoView(){}, animate(){ return {cancel(){},finished:Promise.resolve()}; } };
  return node;
}

function boot(){
  const doc={ createElement:el, createElementNS:el, createTextNode:t=>({textContent:t}),
    body:el(), documentElement:el(), head:el(), hidden:false,
    querySelector(){ return el(); }, querySelectorAll(){ return []; },
    getElementById(){ return el(); }, addEventListener(){}, removeEventListener(){},
    hasFocus(){ return true; }, visibilityState:'visible' };
  const store={};
  const sandbox={ window:null, document:doc, console:{log(){},warn(){},error(){},debug(){}},
    innerWidth:3072, innerHeight:1920, devicePixelRatio:1, screen:{width:3072,height:1920},
    location:{ search:'', href:'http://x/', origin:'http://x' }, URLSearchParams,
    localStorage:{ getItem:k=>store[k]??null, setItem:(k,v)=>{store[k]=String(v);}, removeItem:k=>{delete store[k];} },
    matchMedia:()=>({matches:false,addEventListener(){},addListener(){},removeListener(){}}),
    setTimeout:(f,t)=>setTimeout(f,Math.min(t||0,5)), clearTimeout, setInterval:()=>0, clearInterval,
    requestAnimationFrame:f=>setTimeout(f,0), cancelAnimationFrame:clearTimeout,
    navigator:{ userAgent:'node', platform:'linux', onLine:true },
    fetch:()=>new Promise(()=>{}),                 // never resolves: the pre-data state on purpose
    Intl, Date, Math, JSON, isNaN, parseFloat, parseInt, Object, Array, String, Number, Boolean,
    Promise, Error, RegExp, Set, Map, encodeURIComponent, decodeURIComponent, btoa:s=>s, atob:s=>s,
    __PC:{ $:()=>el(), $$:()=>[], enc:String, toast(){}, VIEW:'home', isView:()=>false,
           viewer:()=>({pubkey:'a'.repeat(64)}), relayQuery:async()=>[], authFetch:()=>new Promise(()=>{}) },
    ClientSettings:{ get:(k,d)=>d, set(){} } };
  sandbox.window=sandbox; sandbox.globalThis=sandbox; sandbox.self=sandbox;
  vm.runInNewContext(src, sandbox, {timeout:8000});
  return sandbox;
}

(async () => {
  let w;
  try{ w = boot(); }catch(e){ console.log(JSON.stringify({error:'boot: '+String(e&&e.message||e).slice(0,200)})); return; }
  const PCOS = w.PCOS || w.window.PCOS;
  if(!PCOS || !PCOS.__widgets){ console.log(JSON.stringify({error:'no __widgets export'})); return; }
  const reg = PCOS.__widgets();
  const out = {};
  for(const key of Object.keys(reg)){
    const def = reg[key];
    const tile = el();
    const rec = { mounted:false, threw:null, drew:0, label:!!(def && def.label), refreshThrew:null };
    try{
      if(typeof def.mount !== 'function'){ rec.threw='no mount()'; out[key]=rec; continue; }
      def.mount(tile, { key, size:'m', cfg:{} });
      rec.mounted = true;
      rec.drew = String(tile.innerHTML || '').length;
    }catch(e){ rec.threw = String(e && e.message || e).slice(0,120); }
    /* BOUNDED. A widget's refresh awaits the network, and the stub never resolves — which is the
       state being tested (a tile must not be blank while its data is in flight). Awaiting it
       unbounded empties node's event loop and the process exits with NO OUTPUT AT ALL, which reads
       exactly like a passing run. */
    try{
      if(typeof def.refresh === 'function')
        await Promise.race([Promise.resolve(def.refresh(tile, { key, size:'m', cfg:{} })),
                            new Promise(r => setTimeout(r, 150))]);
    }catch(e){ rec.refreshThrew = String(e && e.message || e).slice(0,120); }
    rec.after = String(tile.innerHTML || '').length;
    out[key] = rec;
  }
  console.log(JSON.stringify(out));
})().catch(e=>console.log(JSON.stringify({error:'run: '+String(e&&e.message||e).slice(0,200)})));
