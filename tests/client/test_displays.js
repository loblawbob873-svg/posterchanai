'use strict';
const assert=require('assert'), os=require('os'), fs=require('fs'), path=require('path');
const {Displays,validate,commands,modeText}=require('../../desktop/displays.js');
const {WayfireWM,normalizeOutput,randrHead}=require('../../desktop/wm-wayfire.js');
const live=[
 {name:'eDP-1',active:true,focused:true,rect:{x:0,y:0,width:1920,height:1080},scale:1,transform:'normal',
  modes:[{width:1920,height:1080,refresh:60000,current:true,preferred:true}]},
 {name:'DP-1',active:true,focused:false,rect:{x:1920,y:0,width:2560,height:1440},scale:1,transform:'normal',
  modes:[{width:2560,height:1440,refresh:144000,current:true,preferred:true}]}
];
const rows=[
 {name:'eDP-1',enabled:true,x:0,y:360,mode:'1920x1080@60Hz',scale:1,transform:'normal',primary:true},
 {name:'DP-1',enabled:true,x:1920,y:0,mode:'2560x1440@144Hz',scale:1,transform:'normal'}
];
assert.deepStrictEqual(validate(rows,live).map(x=>x.name),['eDP-1','DP-1']);
assert(commands(validate(rows,live))[1].includes('pos 1920 0'));
assert(commands(validate(rows,live)).includes('workspace 1 output "eDP-1"'));
assert(commands(validate(rows,live)).includes('focus output "eDP-1"'));
assert.throws(()=>validate([{name:'DP-9'}],live),/unknown/);
assert.throws(()=>validate(rows.map(x=>Object.assign({},x,{enabled:false})),live),/at least one/);
const negative=validate([
 {name:'eDP-1',enabled:true,x:-1530,y:290,scale:1,transform:'normal'},
 {name:'DP-1',enabled:true,x:2340,y:270,scale:1,transform:'normal'}
],live);
assert.deepStrictEqual(negative.map(x=>[x.name,x.x,x.y]),[
 ['eDP-1',0,20],['DP-1',3870,0]
]);
assert(commands(negative)[0].includes('pos 0 20'));
assert(commands(negative)[1].includes('pos 3870 0'));

const normalized=normalizeOutput({id:7,name:'DP-3',active:false,geometry:{x:3,y:4,width:5,height:6},
 modes:[{width:3840,height:2160,refresh_rate:59940,is_current:true,is_preferred:true}]});
assert.deepStrictEqual(normalized.modes,[{width:3840,height:2160,refresh:59940,current:true,preferred:true}]);

/* One stub for BOTH wlr-randr calls this backend makes: `--json` to read the heads and a repeated
 * --output transaction to write them. `json` is what the read answers; every other invocation is a
 * write and is recorded for the argv assertions below. */
function fakeSpawn(seen,exitCode=0,error=null,json=null){return (program,argv)=>{
  const {EventEmitter}=require('events'),child=new EventEmitter();
  child.stderr=new EventEmitter();child.stdout=new EventEmitter();
  const reading=argv&&argv.length===1&&argv[0]==='--json';
  if(!reading)seen.push({program,argv});
  process.nextTick(()=>{
    if(error&&!reading)return child.emit('error',error);
    if(reading){
      if(json==null)return child.emit('close',1);         // wlr-randr absent: read must degrade
      child.stdout.emit('data',Buffer.from(JSON.stringify(json)));
      return child.emit('close',0);
    }
    child.emit('close',exitCode);
  });
  return child;
};}

/* wlr-randr prints refresh as a float32 of MILLIhertz/1000, so 59999 mHz round-trips as 59.999001.
 * Reading it as-is made a 60Hz monitor advertise "@0.06Hz" and every saved mode unmatchable. */
const rrHead=randrHead({name:'eDP-1',enabled:true,make:'QHX',model:'GF005',position:{x:0,y:0},
  scale:1.5,transform:'270',modes:[{width:1920,height:1080,refresh:59.999001,current:true,preferred:true}]});
assert.strictEqual(rrHead.modes[0].refresh,59999,'wlr-randr Hz must become the millihertz everything else speaks');
assert.strictEqual(modeText(rrHead.modes[0]),'1920x1080@59.999Hz');
assert.strictEqual(rrHead.scale,1.5);
assert.strictEqual(rrHead.transform,'270');

const wideLive=[
 {name:'DP-1',active:true,rect:{x:0,y:20,width:3840,height:2560},scale:1,modes:[]},
 {name:'DP-2',active:true,rect:{x:3870,y:0,width:3840,height:2560},scale:1,modes:[]}
];
const joined=validate([
 {name:'DP-1',enabled:true,x:0,y:20,scale:1,transform:'normal'},
 {name:'DP-2',enabled:true,x:3870,y:0,scale:1,transform:'normal'}
],wideLive);
assert.strictEqual(joined[1].x,3840, 'small horizontal gaps become pointer walls');

const scaledSeam=validate([
 {name:'eDP-1',enabled:true,x:0,y:0,mode:'1920x1080@60Hz',scale:1.25,transform:'normal'},
 {name:'DP-1',enabled:true,x:1920,y:0,mode:'2560x1440@144Hz',scale:1,transform:'normal'}
],live);
assert.strictEqual(scaledSeam[1].x,1536,
  'changing monitor zoom must preserve the seam so the pointer can cross outputs');

(async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'pc-display-'));
 const seen=[]; const wm={outputs:async()=>JSON.parse(JSON.stringify(live)),command:async c=>seen.push(c)};
 const d=new Displays(wm,{file:path.join(dir,'outputs.conf'),revertMs:10000});
 const p=await d.preview(rows);
 assert(seen.some(x=>x.includes('output "DP-1"')));
 await d.confirm(p.token);
 assert(fs.readFileSync(path.join(dir,'outputs.conf'),'utf8').includes('2560x1440@144Hz'));
 const p2=await d.preview(rows); await d.revert(p2.token);
 assert(seen.slice(-4).some(x=>x.includes('pos 1920 0')));
 const repairDir=fs.mkdtempSync(path.join(os.tmpdir(),'pc-display-repair-'));
 const repairSeen=[];
 const repairWm={outputs:async()=>JSON.parse(JSON.stringify(wideLive)),command:async c=>repairSeen.push(c)};
 const repair=new Displays(repairWm,{file:path.join(repairDir,'outputs.conf')});
 const fixed=await repair.repairPointerGaps();
 assert.strictEqual(fixed.changed,true);
 assert(repairSeen.some(x=>x.includes('"DP-2" enable pos 3840 0')));
 assert(fs.readFileSync(path.join(repairDir,'outputs.conf'),'utf8').includes('"DP-2" enable pos 3840 0'));
 /* A prior live repair can hide an old broken file until reboot. Reconcile the file even when the
  * compositor is already correct, without needlessly reconfiguring active outputs. */
 const savedDir=fs.mkdtempSync(path.join(os.tmpdir(),'pc-display-saved-'));
 const savedFile=path.join(savedDir,'outputs.conf');
 fs.writeFileSync(savedFile,'output "DP-2" enable pos 3870 0 scale 1 transform normal\n');
 const goodLive=JSON.parse(JSON.stringify(wideLive)); goodLive[1].rect.x=3840;
 const savedSeen=[];
 const savedRepair=new Displays({outputs:async()=>goodLive,command:async c=>savedSeen.push(c)},{file:savedFile});
 const savedFixed=await savedRepair.repairPointerGaps();
 assert.strictEqual(savedFixed.changed,true);
 assert.strictEqual(savedSeen.length,0, 'correct live outputs must not flicker during file migration');
 assert(fs.readFileSync(savedFile,'utf8').includes('"DP-2" enable pos 3840 0'));


 /* WAYFIRE ANSWERS NAME/ID/GEOMETRY AND NOTHING ELSE (measured against 0.10.1): no modes, no
  * scale, no transform, no make/model, and NO ROW AT ALL for a head that is switched off. Every
  * one of those is a control on the Displays page, so the merge below is the page working. */
 const wfHeads=[
  {name:'eDP-1',enabled:true,make:'Acme',model:'X1',serial:'S1',position:{x:0,y:0},scale:1,transform:'normal',
   modes:[{width:1920,height:1080,refresh:59.999001,current:true,preferred:true},
          {width:1280,height:720,refresh:60.0,current:false,preferred:false}]},
  {name:'DP-1',enabled:true,make:'Acme',model:'X2',serial:'S2',position:{x:1920,y:0},scale:1,transform:'normal',
   modes:[{width:2560,height:1440,refresh:143.998993,current:true,preferred:true}]},
  {name:'HDMI-A-1',enabled:false,make:'Acme',model:'OFF',serial:'S3',position:{x:0,y:0},scale:1,transform:'normal',
   modes:[{width:1920,height:1080,refresh:60.0,current:false,preferred:true}]}];
 const wayfireCalls=[];
 const wayfire=new WayfireWM('/tmp/not-used',{spawn:fakeSpawn(wayfireCalls,0,null,wfHeads)});
 /* Wayfire's own list, deliberately stripped the way the compositor really returns it. */
 wayfire.outputs=async()=>[
  {name:'eDP-1',active:true,primary:false,focused:false,current_workspace:'1',scale:1,transform:'normal',
   make:'',model:'',serial:'',rect:{x:0,y:0,width:1920,height:1080},id:1,modes:[]},
  {name:'DP-1',active:true,primary:false,focused:false,current_workspace:'2',scale:1,transform:'normal',
   make:'',model:'',serial:'',rect:{x:1920,y:0,width:2560,height:1440},id:3,modes:[]}];
 wayfire._send=async m=>m==='window-rules/get-focused-output'?{result:'ok',info:{name:'DP-1'}}:{};

 const merged=await wayfire.outputsDetailed();
 assert.strictEqual(merged.length,3,'a switched-off head must still be listed, or it can never be re-enabled');
 const mEdp=merged.find(x=>x.name==='eDP-1');
 assert.strictEqual(mEdp.modes.length,2,'the mode menu is empty without the wlr-randr merge');
 assert.strictEqual(mEdp.modes[0].refresh,59999);
 assert.strictEqual(mEdp.make,'Acme','a monitor labelled by its connector is the un-merged read');
 assert.deepStrictEqual(mEdp.rect,{x:0,y:0,width:1920,height:1080},
  'Wayfire owns the logical rectangle: views are placed in it, so the merge must not overwrite it');
 assert.strictEqual(merged.find(x=>x.name==='HDMI-A-1').active,false);
 assert.strictEqual(merged.find(x=>x.name==='DP-1').primary,true,
  'primary comes from get-focused-output; list-outputs never reports focus');

 const wfDir=fs.mkdtempSync(path.join(os.tmpdir(),'pc-display-wayfire-'));
 const wfFile=path.join(wfDir,'displays.json');
 const wfDisplays=new Displays(wayfire,{file:wfFile,revertMs:10000});
 const wfStatus=await wfDisplays.status();
 assert.strictEqual(wfStatus.find(x=>x.name==='eDP-1').modes.length,2,
  'Displays.status must read the detailed outputs, not the bare window IPC');

 /* Built from the MERGED status, so the mode strings are the ones the page would really offer —
  * and the argv below then proves the whole Hz -> millihertz -> Hz round trip, which is the step
  * that silently made every saved mode unmatchable when it was missing. */
 const wfRows=[
  {name:'eDP-1',enabled:true,x:0,y:360,mode:'1920x1080@59.999Hz',scale:1,transform:'normal',primary:true},
  {name:'DP-1',enabled:true,x:1920,y:0,mode:'2560x1440@143.999Hz',scale:1,transform:'normal'}];
 const wp=await wfDisplays.preview(wfRows);
 assert.strictEqual(wayfireCalls.length,1,'one atomic wlr-randr transaction applies all heads');
 assert.strictEqual(wayfireCalls[0].program,'wlr-randr');
 assert.deepStrictEqual(wayfireCalls[0].argv,[
  '--output','eDP-1','--on','--mode','1920x1080@59.999','--pos','0,360','--scale','1','--transform','normal',
  '--output','DP-1','--on','--mode','2560x1440@143.999','--pos','1920,0','--scale','1','--transform','normal']);
 await wfDisplays.confirm(wp.token);
 const persisted=JSON.parse(fs.readFileSync(wfFile,'utf8'));
 assert.strictEqual(persisted.outputs[0].primary,true);
 wayfireCalls.length=0;
 const restored=new Displays(wayfire,{file:wfFile});
 assert.strictEqual((await restored.repairPointerGaps()).persisted,true);
 assert.strictEqual(wayfireCalls.length,1,'confirmed Wayfire layout is restored at session startup');
 assert.strictEqual((await restored.status()).find(x=>x.name==='eDP-1').primary,true,
  'primary selection remains stable instead of following transient keyboard focus');

 /* A SAVED LAYOUT MUST SURVIVE THE MONITOR IT NAMES BEING GONE. Strict validation aborted the
  * whole restore, so unplugging one of two displays silently threw away the arrangement for the
  * one still attached. Pruning keeps it; a request typed on the page is still refused. */
 const gone=[{name:'eDP-1',enabled:true,x:0,y:0,scale:1,transform:'normal',mode:'1920x1080@59.999Hz',primary:true},
             {name:'DP-9',enabled:true,x:1920,y:0,scale:1,transform:'normal',mode:'',primary:false}];
 const pruned=validate(gone,merged,{prune:true});
 assert.deepStrictEqual(pruned.map(x=>x.name),['eDP-1']);
 assert.throws(()=>validate(gone,merged),/unknown or duplicate display: DP-9/);
 const staleMode=[{name:'eDP-1',enabled:true,x:0,y:0,scale:1,transform:'normal',mode:'3840x2160@60Hz',primary:true}];
 assert.strictEqual(validate(staleMode,merged,{prune:true})[0].mode,'',
  'a mode the display no longer offers falls back to preferred rather than aborting the restore');
 assert.throws(()=>validate(staleMode,merged),/unsupported mode/);
 assert.throws(()=>validate([{name:'DP-9',enabled:true,x:0,y:0}],merged,{prune:true}),
  /none of the saved displays are connected/);

 /* NO wlr-randr ON THE MACHINE MUST COST THE MODE MENU, NEVER THE DESKTOP. */
 const bare=new WayfireWM('/tmp/not-used',{spawn:fakeSpawn([],0,null,null)});
 bare.outputs=async()=>[{name:'eDP-1',active:true,primary:false,focused:false,current_workspace:'1',
   scale:1,transform:'normal',make:'',model:'',serial:'',rect:{x:0,y:0,width:1920,height:1080},id:1,modes:[]}];
 bare._send=async()=>{throw new Error('no such method');};
 const degraded=await bare.outputsDetailed();
 assert.strictEqual(degraded.length,1);
 assert.deepStrictEqual(degraded[0].modes,[]);

 const disabledCalls=[];
 const disableWM=new WayfireWM('/tmp/not-used',{spawn:fakeSpawn(disabledCalls,0,null,wfHeads)});
 await disableWM.configureOutputs([{name:'DP-9',enabled:false}]);
 assert.deepStrictEqual(disabledCalls[0].argv,['--output','DP-9','--off']);

 const failedWM=new WayfireWM('/tmp/not-used',{spawn:fakeSpawn([],2,null,wfHeads)});
 await assert.rejects(()=>failedWM.configureOutputs(wfRows),/exit 2/);
 console.log('ALL OK');
})().catch(e=>{console.error(e);process.exit(1)});
