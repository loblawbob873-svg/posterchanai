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
  return frontWish();
}).then(()=>{
  console.log('desktop-bottom behavioral simulation: ok');
}).catch(e=>{console.error(e);process.exit(1);});

/* A SURFACE THAT HAS ASKED FOR THE FRONT KEEPS THE KEYBOARD.
 *
 * System Settings, Task Manager, Virtual Machines, Remote Desktop and folders are drawn INSIDE the
 * desktop surface, so focusing the desktop IS focusing them. This guard used to answer that focus
 * by focusing an application again -- measured on the laptop over raw Wayfire IPC as a 1.2ms
 * bounce -- which is "System settings never gets focus" and "social is stuck behind terminal".
 *
 * The wish is re-read AFTER the `windows()` round trip as well, because the renderer publishes it
 * from the same click that produced the focus event; the two race, and a wish that lands during the
 * await must still win. */
async function frontWish(){
  const seen=[]; const q=[];
  let wants=false;
  const g=createDesktopBottomGuard({backend:'wayfire',shellIds:()=>[5,7],
    windows:async()=>rows, focus:async id=>seen.push(id), defer:fn=>q.push(fn),
    wantsFront:(id)=>{ assert.equal(Number(id),7,'the guard must ask about the surface that was focused');
                       return wants; }});

  wants=true;
  g({change:'view-focused',wayfireView:rows[1]});
  assert.equal(q.length,0,'a desktop that asked for the front must not be pushed behind an app');
  assert.deepEqual(seen,[]);

  wants=false;
  g({change:'view-focused',wayfireView:rows[1]});
  assert.equal(q.length,1,'a desktop with nothing of its own on screen still steps aside');
  await q.shift()();
  assert.equal(seen.length,1,'the ordinary path still restores an application');

  // The race: the event arrives before the renderer's IPC, the wish lands during the await.
  seen.length=0; wants=false;
  g({change:'view-focused',wayfireView:rows[1]});
  assert.equal(q.length,1);
  wants=true;
  await q.shift()();
  assert.deepEqual(seen,[],'a wish that landed during the round trip must still win');

  // A guard given no predicate at all behaves exactly as it did before.
  const plain=[]; const pq=[];
  const g2=createDesktopBottomGuard({backend:'wayfire',shellIds:()=>[5,7],windows:async()=>rows,
    focus:async id=>plain.push(id),defer:fn=>pq.push(fn)});
  g2({change:'view-focused',wayfireView:rows[1]});
  await pq.shift()();
  assert.equal(plain.length,1,'without a predicate the old behaviour is unchanged');

  // A predicate that throws is "no wish", never a guard that stops working.
  const boom=[]; const bq=[];
  const g3=createDesktopBottomGuard({backend:'wayfire',shellIds:()=>[5,7],windows:async()=>rows,
    focus:async id=>boom.push(id),defer:fn=>bq.push(fn),
    wantsFront:()=>{ throw new Error('no surface record'); }});
  g3({change:'view-focused',wayfireView:rows[1]});
  await bq.shift()();
  assert.equal(boom.length,1,'a throwing predicate must not disable the guard');
}
