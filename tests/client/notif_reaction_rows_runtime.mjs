// SEVERAL PEOPLE ON ONE POST, AS AMETHYST DRAWS IT (the SHIPPED notifview.js, built with stub deps).
//
// Issue "Change multiple reactions display" (2026-10-21): the grouped row was one sentence naming the FIRST
// reaction's emoji for everyone. Now one card per post with a row per reaction -- zaps (with each face's sats),
// reposts, then each emoji by how many used it -- and a single reaction is still the plain row it always was.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const ctx={window:{},console,Store:{get:()=>({})}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync('static/js/client/notifview.js','utf8'),ctx);
const enc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const names={};for(let i=1;i<=20;i++)names['p'+i]='person'+i;
const reactDisp=e=>{const c=e.content||'';if(c==='+'||c==='')return '❤️';if(/^:[^:\s]+:$/.test(c)){const t=e.tags.find(x=>x[0]==='emoji');return `<img class="emoji-inline" src="${t[2]}">`;}return enc(c);};
const M=ctx.window.PCNotifViewFactory({state:{LOGO:'/logo.png',ME:{pubkey:'me'}},
  $:()=>null,$$:()=>[],NT:()=>({}),_SHORTCODE_STRIP:/x^/,_notifTs:e=>e.created_at,_tipNote:()=>null,_quotesMe:()=>false,_repoTag:()=>'',
  applyEmojis:s=>s,emojiName:(pk,n)=>n,enc,fmtSats:n=>String(n),isReply:()=>false,needEvent(){},needProfile(){},
  profOf:pk=>({name:names[pk]||'',picture:'/av/'+pk+'.png'}),quotedDiv:()=>'<div class="quoted"></div>',reactDisp,
  replyParentId:()=>'',timeAgo:()=>'1m',zapAmount:e=>+(e._sats||0),zapSender:e=>e._from||'',
  Store:{get:()=>({})}});
let n=0;const ev=(kind,pk,content,extra={})=>Object.assign({id:'e'+(++n),kind,pubkey:pk,content,created_at:1000+n,tags:[['e','POST'],['p','me']]},extra);
const list=[
  ev(7,'p1','+'),ev(7,'p2','😂'),ev(7,'p3','😂'),ev(7,'p4','😂'),ev(7,'p5','+'),ev(7,'p2','+'),ev(7,'p11','😂'),   // p2 twice: in both rows
  ev(6,'p6',''),ev(9735,'zapper-service','',{_from:'p7',_sats:21}),ev(9735,'zapper-service','',{_from:'p8',_sats:1000}),
  ev(7,'p9',':pepe:',{tags:[['e','POST'],['p','me'],['emoji','pepe','https://e/pepe.png']]}),
  ev(7,'p10','🔥',{tags:[['e','OTHER'],['p','me']]}),                                                // alone on another post
];
const out=M.notifGrouped(list);
const card=out.find(x=>x.type==='group');assert.ok(card&&card.tgt==='POST','reactions, reposts and zaps on one post are one card');
assert.equal(card.events.length,11);
const solo=out.find(x=>x.id&&x.content==='🔥');assert.ok(solo,'a reaction alone on its post stays a plain notification');
assert.match(M.notifHtml(solo),/reacted 🔥 to your post/);
const html=M.notifHtml(card);
const rows=[...html.matchAll(/<div class="notif-rxrow (\w+)"[^>]*><span class="notif-rxic">(.*?)<\/span><span class="notif-rxfaces">(.*?)<\/span><\/div>/g)]
  .map(m=>({cls:m[1],ic:m[2],pks:[...m[3].matchAll(/data-pk="([^"]+)"/g)].map(x=>x[1]),sats:[...m[3].matchAll(/<b>([^<]+)<\/b>/g)].map(x=>x[1])}));
assert.deepEqual(rows.map(r=>r.cls),['zap','rt','like','like','like'],'zaps, then reposts, then reactions: '+JSON.stringify(rows));
assert.deepEqual(rows[0].pks,['p7','p8'],'a zap face is the SENDER, not the zapper service');assert.deepEqual(rows[0].sats,['21','1000']);
assert.deepEqual(rows[1].pks,['p6']);
assert.equal(rows[2].ic,'😂');assert.deepEqual(rows[2].pks,['p2','p3','p4','p11'],'the most-used reaction first');
assert.equal(rows[3].ic,'❤️');assert.deepEqual(rows[3].pks,['p1','p5','p2'],'a heart is a heart; one person twice is in both rows');
assert.match(rows[4].ic,/pepe\.png/,'a custom emoji reaction draws its picture');
assert.ok(!/reacted/.test(html),'no sentence of names');
assert.match(html,/data-open="POST"/);
assert.match(html,/aria-label="person2, person3, person4, person11"/,'a row says who, for a screen reader');
// More than twelve faces: twelve and a "+N".
const many=[];for(let i=1;i<=15;i++)many.push(ev(7,'p'+i,'+'));
const h2=M.notifHtml(M.notifGrouped(many)[0]);
assert.equal((h2.match(/class="notif-av"/g)||[]).length,12);assert.match(h2,/>\+3</);
console.log('OK');
