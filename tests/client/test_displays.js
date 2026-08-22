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
assert.throws(()=>validate([{name:'DP-9'}],live),/unknown/);
assert.throws(()=>validate(rows.map(x=>Object.assign({},x,{enabled:false})),live),/at least one/);

(async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'pc-display-'));
 const seen=[]; const wm={outputs:async()=>JSON.parse(JSON.stringify(live)),command:async c=>seen.push(c)};
 const d=new Displays(wm,{file:path.join(dir,'outputs.conf'),revertMs:10000});
 const p=await d.preview(rows);
 assert(seen.some(x=>x.includes('output "DP-1"')));
 await d.confirm(p.token);
 assert(fs.readFileSync(path.join(dir,'outputs.conf'),'utf8').includes('2560x1440@144Hz'));
 const p2=await d.preview(rows); await d.revert(p2.token);
 assert(seen.slice(-2).some(x=>x.includes('pos 1920 0')));
 console.log('ALL OK');
})().catch(e=>{console.error(e);process.exit(1)});
