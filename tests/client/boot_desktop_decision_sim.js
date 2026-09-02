/* DID THIS MACHINE COME UP AS A DESKTOP? — the boot decision, run against the shipped os.js.
 *
 * One shell restart during the window-manager work came up as the ordinary single-column client
 * instead of the desktop. It was not reproducible by hand, and that boot's log carried
 * `Network service crashed or was terminated` + `GPU process launch failed` from the PREVIOUS
 * instance's teardown colliding with the new one.
 *
 * `restore()` decides synchronously. `PCOSShell.available()` is a plain read of `_have`, which is
 * null until the ASYNC `detect()` has asked the compositor — so when detect has not answered yet,
 * the PosterChanOS branch is skipped and the fall-through applies the remembered preference AND
 * `fits()`, a SIZE check. Two conditions, both of which that teardown produces at once: a slow
 * compositor socket and a window that has not been given its real size yet.
 *
 * availableNow — what the synchronous available() says at the moment restore() runs.
 * isShell      — what detect() eventually answers, i.e. whether there is really a compositor.
 * detect() ASSIGNS _have in the real module, so available() flips to true after it answers; the
 * stub does the same, because a stub where it never flips models a different bug.
 */
const fs=require('fs'), path=require('path'), vm=require('vm');
const ROOT='/home/verita84/posterchanai';
const src=fs.readFileSync(process.env.PC_OS_JS||path.join(ROOT,'static/js/client/os.js'),'utf8');

async function boot({ availableNow, isShell, osModeRemembered, windowWidth }){
  const store={ osMode: osModeRemembered };
  const el=()=>({ innerHTML:'', style:{}, dataset:{}, className:'',
    classList:{add(){},remove(){},toggle(){},contains(){return false}},
    appendChild(){}, insertAdjacentElement(){}, setAttribute(){}, getAttribute(){return null},
    addEventListener(){}, removeEventListener(){}, querySelector(){return el()}, querySelectorAll(){return []},
    getBoundingClientRect(){return {width:windowWidth,height:900,top:0,left:0,right:windowWidth,bottom:900}},
    remove(){}, focus(){}, closest(){return null} });
  const doc={ createElement:el, body:el(), documentElement:el(), head:el(),
    querySelector(){return el()}, querySelectorAll(){return []},
    addEventListener(){}, removeEventListener(){}, hidden:false, hasFocus(){return true} };
  const sandbox={
    window:null, document:doc, console,
    innerWidth:windowWidth, innerHeight:900, devicePixelRatio:1,
    location:{ search:'', href:'http://x/' }, URLSearchParams,
    localStorage:{ getItem:k=>store[k]==null?null:String(store[k]), setItem:(k,v)=>{store[k]=v}, removeItem:k=>{delete store[k]} },
    matchMedia:()=>({matches:false, addEventListener(){}, addListener(){}}),
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame:f=>setTimeout(f,0), cancelAnimationFrame:clearTimeout,
    navigator:{ userAgent:'node', platform:'linux' }, screen:{ width:windowWidth, height:900 },
    __PC:{ $:()=>el(), $$:()=>[], enc:String, toast(){}, VIEW:'home',
           isView:()=>false, viewer:()=>({}), relayQuery:async()=>[] },
    /* The real osshell.js detect() ASSIGNS _have, so available() answers true afterwards.
       A static stub here would model a shell that never becomes available, which is not the bug. */
    PCOSShell:{ available:()=>availableNow===true,
                detect:async()=>{ if(isShell===true) availableNow=true; return isShell===true; } },
    ClientSettings:{ get:(k,d)=>(store[k]==null?d:store[k]), set:(k,v)=>{store[k]=v} },
  };
  sandbox.window=sandbox; sandbox.globalThis=sandbox; sandbox.self=sandbox;
  try{ vm.runInNewContext(src, sandbox, {timeout:6000}); }catch(e){ return {error:String(e).slice(0,140)}; }
  const PCOS=sandbox.PCOS||sandbox.window.PCOS;
  if(!PCOS||!PCOS.restore) return {error:'no PCOS.restore'};
  try{ PCOS.restore(); }catch(e){ return {error:'restore threw: '+String(e).slice(0,120)}; }
  await new Promise(r=>setTimeout(r,30));            // let a late detect() answer
  const on = !!(PCOS.isOn ? PCOS.isOn() : false);
  return { desktop:on };
}
const cases=[
  ['OS · detect answered, wide  ', {availableNow:true,  isShell:true,  osModeRemembered:true,  windowWidth:3072}],
  ['OS · detect answered, narrow', {availableNow:true,  isShell:true,  osModeRemembered:true,  windowWidth:300}],
  ['OS · detect PENDING, wide   ', {availableNow:false, isShell:true,  osModeRemembered:true,  windowWidth:3072}],
  ['OS · detect PENDING, NARROW ', {availableNow:false, isShell:true,  osModeRemembered:true,  windowWidth:300}],
  ['OS · pending, narrow, nopref', {availableNow:false, isShell:true,  osModeRemembered:false, windowWidth:300}],
  ['browser · wide, pref on     ', {availableNow:false, isShell:false, osModeRemembered:true,  windowWidth:1600}],
  ['browser · wide, no pref     ', {availableNow:false, isShell:false, osModeRemembered:false, windowWidth:1600}],
  ['browser · narrow, pref on   ', {availableNow:false, isShell:false, osModeRemembered:true,  windowWidth:300}],
];
(async()=>{ for(const [n,c] of cases){ const r=await boot(c);
  console.log('  '+n+' -> '+(r.error?('ERROR '+r.error):('desktop='+r.desktop))); } })();
