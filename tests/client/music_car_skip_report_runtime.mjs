/* A SKIP FROM THE CAR SAYS HOW IT WENT. The phone's report (2026-10-24) showed seven ⏭ in seven seconds, every one
 * "answered" -- received -- while the song kept playing: until trackUrl() has fetched and decrypted the next track the
 * old one plays on. Runs the SHIPPED MusicPlayer.play with a slow / failing trackUrl and a stand-in MusicControls
 * plugin: a media-button skip that takes long, or fails, is written into the phone's report (playBlocked); an
 * on-screen skip is not, and a fast one is not. */
import vm from 'node:vm';import assert from 'node:assert/strict';
import { clientSourceAt, installStateGlobals } from './client_source.mjs';
const code=clientSourceAt(process.env.PC_APP_SOURCE||new URL('../../static/js/client/app.js',import.meta.url));
const play=code.slice(code.indexOf('    async play(sha, opts){'),code.indexOf('    toggle(){'));
assert(play.length>200,'play() not found');

async function run({ms=0,fail=false,fromButton=true}){
  const reported=[];
  const ctx={console,Math,Number,Date,Promise,setTimeout,String,
    _audioEl:{src:'old',currentTime:0,duration:200,paused:false,play:async()=>{}},
    trackUrl:async()=>{await new Promise(r=>setTimeout(r,ms));if(fail)throw new Error('blossom 404');return 'blob:next';},
    _capPlugin:(name,fn)=>name==='MusicControls'&&fn==='playBlocked'?{playBlocked:a=>{reported.push(a.reason);}}:null,
    toast:()=>{}};
  ctx.window=ctx;
  vm.createContext(installStateGlobals(ctx)&&ctx);
  vm.runInContext('globalThis.P={\n'+play+'\nensure(){},_render(){},refreshQueue(){},_nativePush(){return Promise.resolve(true)},'
    +'_dropCover(){},_mediaMeta(){},_readTags(){},el:{classList:{remove(){}}},queue:["a","b"],cur:"a",_history:[],_bag:[],_loading:false};',ctx);
  if(fromButton) ctx.P._pressAt=Date.now();
  await ctx.P.play('b',{force:true});
  return reported;
}
let r=await run({ms:2700});
assert.equal(r.length,1,'a slow skip from the car was not reported: '+JSON.stringify(r));
assert.match(r[0],/took \d+\.\d s to load the next track/);
r=await run({ms:0,fail:true});
assert.equal(r.length,1,'a failed skip from the car was not reported');
assert.match(r[0],/failed: blossom 404/);
assert.deepEqual(await run({ms:0}),[],'a fast skip must not be reported');
assert.deepEqual(await run({ms:2700,fromButton:false}),[],'an on-screen skip is not a car press');
console.log('car skip report passed');
