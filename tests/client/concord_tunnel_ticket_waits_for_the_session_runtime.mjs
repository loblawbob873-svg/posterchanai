// A TICKET ASKED FOR BEFORE THE SESSION EXISTS IS "NOT YET", NOT "NO" (the SHIPPED refreshTunnel).
//
// Concord starts with the page and the app session is minted a moment later, so the first ticket request can
// answer 401. That used to switch the tunnel off for five minutes -- Concord then dialled every outside relay
// directly from the member's own address. Seen on poster.place's post-deploy sign-in check, 2026-10-22.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const src=fs.readFileSync('static/js/client/concord.js','utf8');
const cut=(a,b)=>{const i=src.indexOf(a);assert.ok(i>=0,a);const j=src.indexOf(b,i);assert.ok(j>i,b);return src.slice(i,j);};
const code=cut('  const TUNNEL={','  /* /ws/concord-relay is an APP route')+cut('  function refreshTunnel(){','  function cordVia(p){');
async function run(answers, ensure){
  const asked=[];let ensured=0;
  const ctx={Date,Math,Number,String,setTimeout:()=>0,clearTimeout:()=>{},
    fetch:async url=>{asked.push(url);const a=answers.shift();return {ok:a.status===200,status:a.status,json:async()=>a.body};},
    window:{}};
  const deps={ensureAiSession:async()=>{ensured++;if(!ensure)throw new Error('refused');return {};}};ctx.PC=()=>deps;
  vm.createContext(ctx);vm.runInContext(code+';this.refreshTunnel=refreshTunnel;this.TUNNEL=TUNNEL;',ctx);
  await ctx.refreshTunnel();
  return {T:ctx.TUNNEL,asked:asked.length,ensured};
}
// Not signed in yet: the session is made and the second ask gets the ticket. The tunnel stays ON.
{const r=await run([{status:401},{status:200,body:{ticket:'T1',ttl:3600}}],true);
 assert.equal(r.ensured,1,'the session was never made');
 assert.equal(r.asked,2,'the ticket was not asked for again');
 assert.equal(r.T.ticket,'T1');assert.equal(r.T.off,false,'a 401 before the session switched the tunnel off');}
// The node will not give this key a session (no account here): off, as before -- nothing else can carry it.
{const r=await run([{status:401}],false);
 assert.equal(r.asked,1);assert.equal(r.T.off,true);assert.equal(r.T.ticket,'');}
// Not a member (403) is a real "no", not a race: off without a second ask.
{const r=await run([{status:403}],true);
 assert.equal(r.ensured,0);assert.equal(r.asked,1);assert.equal(r.T.off,true);}
// The ordinary case is one ask.
{const r=await run([{status:200,body:{ticket:'T2',ttl:3600}}],true);
 assert.equal(r.asked,1);assert.equal(r.ensured,0);assert.equal(r.T.ticket,'T2');}
console.log('OK');
