"""Idle cost of the shipped client: timer callbacks (by source line) and relay sends, visible vs 'hidden'.

Run from the repo root:  venv-unified/bin/python /path/measure_idle.py [seconds]
"""
import asyncio
import json
import sys

sys.path.insert(0, '.')
from tests.client import realnode_harness as R  # noqa: E402

SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
CENSUS = r'''(()=>{
 const C=window.__census={timers:{},ws:{},since:Date.now()};
 const where=()=>{const s=(new Error().stack||'').split('\n');for(let i=2;i<s.length;i++){const m=s[i].match(/\/static\/js\/client\/([^?:]+)[^:]*:(\d+)/);if(m)return m[1]+':'+m[2];}return '?';};
 const bump=(o,k)=>{o[k]=(o[k]||0)+1;};
 const wrap=(name)=>{const orig=window[name];window[name]=function(fn,ms,...a){const w=where()+' '+name.replace('set','')+'('+(ms|0)+')';
   if(typeof fn!=='function')return orig.call(this,fn,ms,...a);
   return orig.call(this,function(...b){bump(C.timers,w);return fn.apply(this,b);},ms,...a);};};
 wrap('setTimeout');wrap('setInterval');
 const raf=window.requestAnimationFrame;window.requestAnimationFrame=function(fn){const w=where()+' raf';return raf.call(this,t=>{bump(C.timers,w);fn(t);});};
 const send=WebSocket.prototype.send;WebSocket.prototype.send=function(d){try{const m=JSON.parse(d);bump(C.ws,String(m[0]));}catch(_){bump(C.ws,'?');}return send.call(this,d);};
 window.__censusReset=()=>{C.timers={};C.ws={};C.since=Date.now();};
 window.__hide=()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
   Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>'hidden'});
   document.dispatchEvent(new Event('visibilitychange'));window.dispatchEvent(new Event('pagehide'));};
})();'''


def report(label, c, secs):
    timers = c['timers']
    total = sum(timers.values())
    print('== %s: %d timer callbacks in %ds (%.1f/s); relay sends %s' % (label, total, secs, total / secs, json.dumps(c['ws'])))
    for k, v in sorted(timers.items(), key=lambda x: -x[1])[:15]:
        print('   %5d  %s' % (v, k))


async def main():
    with R.Node() as n:
        async with R.Device(n, 7, pre_init=CENSUS, phone=True) as d:
            await asyncio.sleep(20)                       # startup settles
            await d.b.js('__censusReset();true')
            await asyncio.sleep(SECS)
            report('visible, home timeline, idle', await d.b.js('window.__census'), SECS)
            await d.b.js('__hide();__censusReset();true')
            await asyncio.sleep(SECS)
            report('hidden (backgrounded), idle', await d.b.js('window.__census'), SECS)

asyncio.run(main())
