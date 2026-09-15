"""A late IndexedDB response must not repaint a different account or a newer calendar load."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
CALENDAR = ROOT / "static/js/client/calendar.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _run(body, source=None):
    source = source if source is not None else CALENDAR.read_text()
    cached = source[source.index("    async function loadCached(){"):
                    source.index("    /* Apply a queued write")]
    widget = source[source.index("    async function widgetTick(maxAgeH){"):
                    source.index("    window.PCCalendar =")]
    render = source[source.index("    function render(){"):
                    source.index("    /* KEEP THE HOME-SCREEN WIDGET FED")]
    script = r"""
const assert=require('node:assert/strict');
let currentOwner='alice';
const owner=()=>currentOwner;
const S={owner:'alice',loadGen:1,ready:false,cals:[],items:{},cal:'',rev:0,cached:false,queued:0};
const pictures=[],widgets=[],reads=[];
let loads=0;
const PC={capPlugin:()=>({})};
const paint=()=>pictures.push(JSON.parse(JSON.stringify(S)));
let widgetWait=null;
const pushWidget=async()=>{widgets.push(JSON.parse(JSON.stringify(S)));if(widgetWait)await widgetWait;};
const load=async()=>{loads++;};
const firstOf=date=>date,todayKey=()=> '2026-09-14';
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};
const queue=deferred(),snapshot=deferred();
const CalQueue={read:()=>{reads.push(['queue',owner()]);return queue.promise;}};
const CalCache={read:()=>{reads.push(['snapshot',owner()]);return snapshot.promise;}};
const drain=()=>new Promise(resolve=>setImmediate(resolve));
const oldSnapshot={cals:[{id:'alice-private'}],items:{'alice-private':[{uid:'appointment'}]},at:1};
""" + cached + widget + render + "\n(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
    return subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=5)


@pytest.mark.parametrize("stage", ["queue", "snapshot", "widget", "render"])
@pytest.mark.parametrize("change", ["account", "newer_load"])
def test_delayed_cache_result_cannot_replace_current_state(stage, change):
    result = _run(f"const stage={json.dumps(stage)},change={json.dumps(change)};\n" + r"""
const pending=stage==='widget'?widgetTick():stage==='render'?render():loadCached();
if(stage==='snapshot'||stage==='widget'){queue.resolve([]);await drain();}
assert.equal(reads.length,stage==='snapshot'?2:1,'the intended async boundary was not reached');
if(change==='account'){currentOwner='bob';S.owner='bob';}
else S.loadGen++;
S.queued=7;
const expected=JSON.stringify(S);
queue.resolve([{uid:'alice-pending'}]);snapshot.resolve(oldSnapshot);
await pending;await drain();
assert.equal(JSON.stringify(S),expected,'stale cache data changed the current calendar state');
assert.equal(pictures.length,stage==='render'?1:0,'stale cache data repainted the calendar');
assert.equal(widgets.length,0,'stale cache data reached the home-screen widget');
assert.equal(loads,0,'an abandoned widget refresh started another network load');
if(stage==='queue')assert.equal(reads.length,1,'an abandoned cache load read the next account snapshot');
""")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("widget", [False, True])
def test_current_cache_still_fills_calendar_and_widget(widget):
    result = _run(f"const widget={json.dumps(widget)};\n" + r"""
const pending=widget?widgetTick():loadCached();
queue.resolve([{uid:'offline'}]);snapshot.resolve(oldSnapshot);
await pending;
assert.deepEqual(S.cals,oldSnapshot.cals);assert.deepEqual(S.items,oldSnapshot.items);
assert.equal(S.cached,true);
if(widget){assert.equal(widgets.length,1);assert.equal(loads,1);}
else{assert.equal(pictures.length,1);assert.equal(S.queued,1);assert.equal(S.cal,'alice-private');}
""")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("change", ["account", "newer_load"])
def test_widget_completion_cannot_restart_an_abandoned_refresh(change):
    result = _run(f"const change={json.dumps(change)};\n" + r"""
const pushed=deferred();widgetWait=pushed.promise;
const pending=widgetTick();snapshot.resolve(oldSnapshot);await drain();
assert.equal(widgets.length,1,'the native widget push did not start');
if(change==='account')currentOwner='bob';else S.loadGen++;
pushed.resolve();await pending;
assert.equal(loads,0,'a stale native widget completion started another network load');
""")
    assert result.returncode == 0, result.stdout + result.stderr
