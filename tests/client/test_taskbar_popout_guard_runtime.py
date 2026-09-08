"""Run the renderer guard with its real geometry planner and decorated Social windows."""
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_own_native_apps_keep_taskbar_clickable_across_display_scales():
    source = (ROOT / 'static/js/client/os.js').read_text()
    guard = source[source.index('  let _barSeen = new Map();'):
                   source.index('  /* Whatever the compositor has that we have not framed yet. */')]
    script = '''
const assert=require('node:assert/strict');
const N=require(%s),NAT=()=>N;
let placed=[],area,deskBottom,settleTimer=null,freshRows,refreshes=0;
let on=true,_deskIdle=false;
const setTimeout=(fn,ms)=>{assert.equal(ms,120);settleTimer=fn;return 1};
const clearTimeout=()=>{settleTimer=null};
const adoptAll=async()=>{refreshes++;await _guardTaskbar(freshRows,1)};
const pcWM={place:async(id,x,y,w,h)=>placed.push({id,x,y,w,h}),workArea:async a=>{area=a}};
const window={pcWM,visualViewport:{width:1,height:1}};
const desk={getBoundingClientRect:()=>({bottom:deskBottom})};
const nativeWins=()=>[{native:8}];
''' % json.dumps(str(ROOT / 'static/js/client/osnative.js')) + guard + '''
(async()=>{
 for(const [width,height,x,y] of [[800,600,0,0],[1366,768,-1366,0],
   [3840,2560,0,0],[3840,2560,3840,0],[1080,1920,0,-1920]]){
  for(const scale of [1,1.25,1.5,2])for(const app of
    ['place.poster.desktop','posterchan-desktop','posterchan']){
   window.visualViewport={width:width/scale,height:height/scale};
   deskBottom=height/scale-48;
   const rect={x:x+22,y,width:width-44,height:height+1};
   const social={id:3,app,title:'PosterChan Window — global',rect,above:10,below:32};
   const rows=[
    {id:1,app,title:'PosterChan · Nostr',rect:{x,y,width,height}},
    {id:2,app,title:'PosterChan · Nostr',rect},
    social,
    {id:4,app:'firefox',title:'Browser',rect,above:23,below:29},
    {id:5,app,title:'PosterChan Popup',rect},
    {...social,id:6,fullscreen:true},
    {...social,id:7,stashed:true},
    {...social,id:8}, // hosted: nsync already owns its geometry
    {...social,id:9,rect:{...rect,x:x+width+22}}, // another output
   ];
   _barSeen=new Map();placed=[];
   freshRows=rows;refreshes=0;
   await _guardTaskbar(rows,1);
   assert.deepEqual(placed,[],'the guard must wait for settled geometry');
   assert.equal(typeof settleTimer,'function','quiet mapping needs a scheduled second sample');
   await settleTimer(); // No compositor event or taskbar click occurs after the window maps.
   assert.equal(refreshes,1);
   assert.equal(settleTimer,null,'the settled window must not start a permanent poll');
   assert.deepEqual(placed.map(p=>p.id),[3,4],'own Social and external apps must both fit');
   assert.equal(area.reserve,Math.round(48*scale));
   for(const p of placed){
    const original=rows.find(r=>r.id===p.id);
    assert.equal(p.x,rect.x);assert.equal(p.w,rect.width);
    assert(p.y-original.above>=y,'title bar left the top of the monitor');
    assert.equal(p.y+p.h+original.below,y+height-area.reserve,
      'decorated app frame still covers Start/notifications');
    original.rect={x:p.x,y:p.y,width:p.w,height:p.h};
   }
   placed=[];
   await _guardTaskbar(rows,1);await _guardTaskbar(rows,1);
   assert.deepEqual(placed,[],'settled correction must not keep resizing apps');
   assert.equal(settleTimer,null);
  }
 }
 // The delayed sample must use current geometry, not replay the first map event.
 freshRows[2].rect.height+=300;placed=[];
 await _guardTaskbar(freshRows,1);
 freshRows[2].rect.height+=20;
 await settleTimer();
 assert.deepEqual(placed,[],'a still-changing window is not settled');
 assert.equal(typeof settleTimer,'function');
 await settleTimer();
 assert.deepEqual(placed.map(p=>p.id),[3]);
 assert.equal(settleTimer,null);
 // Switching out of the OS or idling it must not cause a delayed adoption pass.
 for(const state of ['off','idle']){
  _barSeen=new Map();await _guardTaskbar(freshRows,1);
  const before=refreshes;
  on=state!=='off';_deskIdle=state==='idle';
  await settleTimer();
  assert.equal(refreshes,before);assert.equal(_barSettleT,0);
  on=true;_deskIdle=false;
 }
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
