// SETTINGS -> APPEARANCE, the painter (the SHIPPED apptheme.js in a fake document).
//
// Git issue "Concord Theme match with app theme" (2026-10-21): your colours for the whole app, a section's own
// colours, a wallpaper at nine positions or tiled. Pinned: the CSS outranks every preset, a section's colours
// apply by attribute only on its views, the wallpaper becomes the page canvas (what body and the sticky bars
// paint), an image address cannot break out of url(""), each account keeps its own look, and the newer copy
// wins a sync either way.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const mem={};const attrs={};let style=null;const saved=[];
const head={children:[],appendChild(e){e.parentNode=head;this.children=this.children.filter(x=>x!==e);this.children.push(e);return e;},get lastElementChild(){return this.children[this.children.length-1];}};
const document={head,documentElement:{setAttribute:(k,v)=>attrs[k]=v,removeAttribute:k=>delete attrs[k]},
  getElementById:id=>id==='pc-look'?style:null,createElement:()=>(style={id:'',textContent:'',remove(){style=null;}})};
const ctx={window:{__PC:{saveAppLook:v=>saved.push(v)}},document,localStorage:{getItem:k=>mem[k]??null,setItem:(k,v)=>mem[k]=String(v)},console,Date,JSON,Math,Number,String,Object,Set,isFinite};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/js/client/apptheme.js','utf8'),ctx);
const L=ctx.window.PCLook;assert.ok(L,'apptheme.js did not publish PCLook');
assert.equal(style,null,'nothing custom = no stylesheet at all');
// An edit made while signed in but BEFORE the synced prefs arrive survives the restore's forOwner().
ctx.window.__PC.me=()=>({pubkey:'a'.repeat(64)});
L.set({accent:'#ff0000',bg:'#ffffff'});
L.forOwner('a'.repeat(64));
assert.equal(L.get().accent,'#ff0000','the prefs restore threw away an edit made just before it');
let css=style.textContent;
assert.match(css,/^:root:root:root\{/,'outranks :root[data-theme=x]');
assert.match(css,/--neon:#ff0000/);assert.match(css,/--accent-rgb:255,0,0/);assert.match(css,/--bg:#ffffff/);
assert.match(css,/--text:#1b1f2e/,'a pale background gets dark text');assert.match(css,/color-scheme:light/);
assert.equal(saved.length,1,'a saved change goes to the account');
// A section's own colours, by attribute.
L.setSection('concord',{bg:'#101010'});
css=style.textContent;assert.match(css,/:root:root:root\[data-pcsec="concord"\]\{[^}]*--bg:#101010/);
assert.match(css,/\[data-pcsec="concord"\]\{[^}]*--text:#edeefa/,'a dark section background gets light text');
assert.equal(L.section('concord'),'concord');assert.equal(attrs['data-pcsec'],'concord');
assert.equal(L.section('home'),'social');assert.equal(L.section('nonsense'),'');assert.ok(!('data-pcsec' in attrs));
// Wallpaper: the canvas, every position, tile.
L.set({wall:{url:'https://cdn.example/w.jpg',pos:'right bottom',size:'fill',dim:.5}});
css=style.textContent;
assert.match(css,/--canvas:linear-gradient\(rgba\(255,255,255,0\.5\),rgba\(255,255,255,0\.5\)\),url\("https:\/\/cdn\.example\/w\.jpg"\) right bottom \/ cover no-repeat,#ffffff/);
L.set({wall:{...L.get().wall,pos:'tile'}});assert.match(style.textContent,/url\("[^"]+"\) left top \/ auto repeat/);
L.set({wall:{...L.get().wall,pos:'center top',size:'original'}});assert.match(style.textContent,/center top \/ auto no-repeat/);
assert.equal(L.POSITIONS.length,10,'nine anchors and tile');
for(const bad of ['javascript:alert(1)','https://x/a.jpg") ;}body{display:none','https://x/a b.jpg',"https://x/a'.jpg",'data:image/png;base64,AAAA']){
  const t=L.set({wall:{url:bad,pos:'tile'}});assert.equal(t.wall,null,'refused: '+bad);}
assert.ok(!/display:none/.test(style.textContent));
// Nonsense colours never reach the CSS.
L.set({accent:'red;}body{x:y',bg:'#12345'});assert.ok(!/body\{x/.test(style.textContent));
// Each account its own look.
L.set({accent:'#00ff00',bg:'',wall:null,sec:{}});
L.forOwner('b'.repeat(64));assert.equal(L.get().accent,'','another account does not inherit the last one\'s look');
assert.equal(style,null);
L.forOwner('a'.repeat(64));assert.equal(L.get().accent,'#00ff00','and the first account gets its own back');
// Sync: newer remote wins; an older remote is answered with ours.
const mine=L.get();saved.length=0;
L.adopt({...mine,accent:'#0000ff',at:mine.at-1000},'a'.repeat(64));assert.equal(L.get().accent,'#00ff00');assert.equal(saved.length,1,'an older copy elsewhere is overwritten with ours');
L.adopt({...mine,accent:'#0000ff',at:mine.at+1000},'a'.repeat(64));assert.equal(L.get().accent,'#0000ff');
L.adopt({...mine,accent:'#abcdef',at:mine.at+5000},'c'.repeat(64));assert.equal(L.get().accent,'#0000ff','another account\'s copy is ignored');
L.reset();assert.equal(L.isCustom(),false);
console.log('OK');
