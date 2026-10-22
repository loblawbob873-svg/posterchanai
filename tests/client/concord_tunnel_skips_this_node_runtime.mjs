// THIS NODE UNDER ANOTHER NAME IS NEVER TUNNELLED TO (the SHIPPED cordVia).
//
// poster.place advertises wss://poster.place/relay and also answers as wss://relay.poster.place and
// wss://poster.place/git (the GRASP relay its repositories announce). Tunnelled, those dialled the node from itself
// and were refused 403 -- 12 of 12 in production, 2026-10-21 -- so the PosterChan repository's issues never
// loaded in its Concord room ("clicking bugs in the posterchan room does nothing"). Anybody else's relay still goes
// through the tunnel, including one whose name merely ends in the same letters.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const src=fs.readFileSync('static/js/client/concord.js','utf8');
const cut=(a,b)=>{const i=src.indexOf(a);assert.ok(i>=0,a);const j=src.indexOf(b,i);assert.ok(j>i,b);return src.slice(i,j);};
const code=[cut('  const TUNNEL={','  function refreshTunnel(){'),cut('  function cordVia(p){','  /* The two doors'),
  cut('  function homeRelayUrl(p){','\n',),src.slice(src.indexOf('  function normalizeRelay(value){'),src.indexOf('\n',src.indexOf('  function normalizeRelay(value){')))].join('\n');
const ctx={URL,Date,window:{__PC_API_BASE__:'https://poster.place'},refreshTunnel(){},PC:()=>null};
vm.createContext(ctx);vm.runInContext(code+';this.cordVia=cordVia;this.TUNNEL=TUNNEL;',ctx);
ctx.TUNNEL.ticket='T';ctx.TUNNEL.until=Date.now()+60000;
const via=ctx.cordVia({homeRelay:()=>'wss://poster.place/relay'});
assert.ok(via,'the tunnel must be on for this instance');
for(const own of ['wss://poster.place/relay','wss://relay.poster.place','wss://poster.place/git','wss://relay.poster.place/'])
  assert.equal(via(own),own,own+' is this node and must be dialled directly');
for(const other of ['wss://poster.place:8443/relay','wss://relay.damus.io','wss://notposter.place','wss://poster.place.evil.example','wss://jskitty.com/nostr'])
  assert.ok(via(other).startsWith('wss://poster.place/ws/concord-relay?u='),other+' must still go through the tunnel');
console.log('OK');
