import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = fs.readFileSync(process.argv[2] || new URL('../../static/js/client/term.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('    function _withLocal('), source.indexOf('    function _mountTerm('));
const remote = {name:'server',label:'user@server',keyed:true};
function setup(local=true){
  let reply=async()=>({ok:true,status:200,json:async()=>({hosts:[remote]})}), key='alice', auth=async()=>{};
  const timers=[], selection={value:'server',innerHTML:''};
  const ctx=vm.createContext({
    window:{__PC_API_BASE__:'https://instance.example'}, LOCAL:()=>local?{}:null,
    LOCAL_HOST:{name:'local',label:'this computer'},hosts:[],mounted:{},
    PC:{viewer:()=>({pubkey:key}),ensureAiSession:()=>auth()},
    authFetch:()=>reply(),$:()=>selection,enc:String,_state:()=>{},
    setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout:()=>{},
  });
  vm.runInContext(code,ctx);
  return {ctx,timers,reply:fn=>reply=fn,auth:fn=>auth=fn,key:value=>key=value,
    names:()=>Array.from(ctx.hosts,h=>h.name)};
}
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};

// Reopening used to clear the list and a permanent _hostsAsked latch prevented any recovery.
{
  const t=setup();
  assert.equal(await t.ctx.loadHosts(),true);await flush();
  assert.deepEqual(t.names(),['local','server']);
  t.reply(async()=>{throw Error('relay unavailable');});
  assert.equal(await t.ctx.loadHosts(),true);await flush();
  assert.deepEqual(t.names(),['local','server']);
  t.reply(async()=>({ok:true,status:200,json:async()=>({hosts:[remote,{name:'new',label:'new host'}]})}));
  await t.ctx._hostsRefresh();
  assert.deepEqual(t.names(),['local','server','new']);
}
// Neither expired authentication nor malformed replies mean the operator deleted their hosts.
for(const local of [false,true]){
  const t=setup(local);await t.ctx.loadHosts();await flush();
  for(const response of [{ok:false,status:401,json:async()=>({detail:'sign in'})},
                         {ok:false,status:503,json:async()=>({})},
                         {ok:true,status:200,json:async()=>({unexpected:true})}]){
    t.reply(async()=>response);await t.ctx.loadHosts();await flush();
    assert(t.names().includes('server'));
  }
  t.reply(async()=>({ok:true,status:200,json:async()=>({hosts:[]})}));
  await t.ctx._hostsRefresh();assert.deepEqual(t.names(),local?['local']:[]);
}
// A sleeping signer never delays the local PTY; its timeout schedules another discovery attempt.
{
  const t=setup();t.auth(()=>new Promise(()=>{}));
  assert.equal(await t.ctx.loadHosts(),true);assert.deepEqual(t.names(),['local']);
  t.timers.find(t=>t.ms===6000).fn();await flush();
  const retry=t.timers.findLast(t=>t.ms===30000);assert(retry);
  t.auth(async()=>{});retry.fn();await flush();assert(t.names().includes('server'));
}
// An old account's late response cannot disclose its host list after an account switch.
{
  const t=setup();let release;
  t.reply(()=>new Promise(resolve=>release=resolve));
  await t.ctx.loadHosts();await flush();t.key('bob');
  t.reply(async()=>({ok:true,status:200,json:async()=>({hosts:[]})}));
  await t.ctx.loadHosts();await flush();
  release({ok:true,status:200,json:async()=>({hosts:[remote]})});await flush();
  assert.deepEqual(t.names(),['local']);
}
console.log('ok: terminal host remount, failure preservation, signer recovery and account isolation');
