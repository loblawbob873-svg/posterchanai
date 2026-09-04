'use strict';
const assert=require('assert');
const {pickDesktopSibling,createDesktopBottomGuard}=require('../../desktop/desktop-bottom.js');
const rows=[
  {id:5,workspace:'2',focusTime:999},
  {id:7,workspace:'3',focusTime:999},
  {id:11,workspace:'3',focusTime:10},
  {id:13,workspace:'3',focusTime:30},
  {id:16,workspace:'3',focusTime:20},
  {id:18,workspace:'3',focusTime:40,stashed:true},
  {id:20,workspace:'2',focusTime:50}
];
assert.equal(pickDesktopSibling(rows,[5,7],rows[1]).id,13,
  'restore the latest visible sibling on the clicked desktop output');

let focused=[];const deferred=[];
const guard=createDesktopBottomGuard({backend:'wayfire',shellIds:()=>[5,7],windows:async()=>rows,
  focus:async id=>focused.push(id),defer:fn=>deferred.push(fn)});
guard({change:'view-focused',wayfireView:rows[1]});
guard({change:'view-focused',wayfireView:rows[1]}); // event storms must not duplicate focus IPC
assert.equal(deferred.length,1);
deferred.shift()().then(()=>{
  assert.deepEqual(focused,[13]);
  guard({change:'view-focused',wayfireView:rows[2]});
  assert.equal(deferred.length,0,'ordinary app focus is never intercepted');
  // A real click changes Wayfire's focus timestamp; the next background/taskbar interaction must
  // restore that newly authoritative app, not a fixed application or an always-on-top exception.
  rows[2].focusTime=60;
  guard({change:'view-focused',wayfireView:rows[1]});
  assert.equal(deferred.length,1);
  return deferred.shift()();
}).then(()=>{
  assert.deepEqual(focused,[13,11]);
  console.log('desktop-bottom behavioral simulation: ok');
}).catch(e=>{console.error(e);process.exit(1);});
