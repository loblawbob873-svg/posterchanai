"""Exercise the real launcher scroll latch against delayed restores and user scrolling."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_top_latch_cancels_old_restores_without_fighting_user_scroll():
    source = (ROOT / 'static/js/client/app.js').read_text()
    body = source[source.index('  function timelineTop(view){'):source.index('  function setMobileNav(')]
    script = '''
const assert=require('node:assert/strict');
let VIEW='global',_tlForceTop='',_scrollRestoreGen=7,hidden=new Set();
const _TL_TABS=['home','global','trending'],_tlScrollMemo={global:500,home:300};
const feed={scrollTop:500,listeners:{},addEventListener(k,fn){this.listeners[k]=fn}};
const calls=[],timers=[],frames=[],$=()=>feed,tlHiddenSet=()=>hidden,_startTimeline=()=> 'global';
const Relay={reviveStale:()=>calls.push('revive')};
const switchView=v=>{VIEW=v;calls.push('switch:'+v)},renderView=()=>calls.push('render');
const requestAnimationFrame=fn=>frames.push(fn),setTimeout=fn=>timers.push(fn);
'''+body+'''
const oldGeneration=_scrollRestoreGen;
timelineTop('global');
assert.deepEqual(calls,['revive','render']);assert.equal(feed.scrollTop,0);
assert.equal(_tlScrollMemo.global,undefined);assert.notEqual(_scrollRestoreGen,oldGeneration);
// A pending layout restoration may write its old pixel offset. The top latch wins initially.
feed.scrollTop=500;timers[0]();assert.equal(feed.scrollTop,0);
while(frames.length)frames.shift()();assert.equal(feed.scrollTop,0);
// Genuine input cancels every later callback; reading must never be dragged back up.
feed.listeners.wheel();feed.scrollTop=240;
for(const timer of timers)timer();assert.equal(feed.scrollTop,240);
// If both configurable timelines are hidden, the launcher must open visible Trending once.
calls.length=0;hidden=new Set(['home','global']);timelineTop('home');
assert.deepEqual(calls,['revive','switch:trending']);assert.equal(feed.scrollTop,0);
// An unrelated navigation also protects its new scroll offset from outstanding callbacks.
VIEW='messages';feed.scrollTop=90;for(const timer of timers)timer();assert.equal(feed.scrollTop,90);
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
