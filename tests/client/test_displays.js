'use strict';
const assert=require('assert'), os=require('os'), fs=require('fs'), path=require('path');
const {Displays,validate,commands}=require('../../desktop/displays.js');
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
 console.log('ALL OK');
})().catch(e=>{console.error(e);process.exit(1)});
