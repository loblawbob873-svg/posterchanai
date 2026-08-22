'use strict';
const assert=require('assert');
const S=require('../../desktop/shell-displays.js');

const outs=[
  {name:'DP-1',active:true,focused:false,rect:{x:1920,y:0,width:2560,height:1440}},
  {name:'eDP-1',active:true,focused:true,rect:{x:0,y:360,width:1920,height:1080}},
  {name:'HDMI-A-1',active:false,rect:{x:4480,y:0,width:1920,height:1080}},
];
const p=S.plan(outs,[{name:'1',output:'eDP-1'}]);
assert.deepStrictEqual(p.map(x=>[x.output,x.workspace]),[['eDP-1','1'],['DP-1','2']]);
assert.strictEqual(p[0].primary,true);
assert.deepStrictEqual(S.windowsFor([
  {id:1,workspace:'1'},{id:2,workspace:'2'},{id:3,workspace:'__i3_scratch',stashed:true}
],'2').map(x=>x.id),[2,3]);
const cmds=S.placement(88,p[1]);
assert(cmds.includes('move workspace to output "DP-1"'));
assert(cmds.includes('[con_id=88] move container to workspace number 2'));
assert(cmds.includes('[con_id=88] floating disable'));
assert.throws(()=>S.placement(0,p[0]),/invalid/);
console.log('ALL OK');
