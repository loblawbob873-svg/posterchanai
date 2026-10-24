"""Where a (phone-speed) client startup spends its time: milestones + CPU self-time by script and function.

Run from the repo root:  venv-unified/bin/python /path/measure_startup.py [cpu_slowdown]
"""
import asyncio
import collections
import sys

sys.path.insert(0, '.')
from tests.client import realnode_harness as R  # noqa: E402

SLOW = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1][0].isdigit() else 4.0


async def main():
    with R.Node() as n:
        async with R.Device(n, 7, phone=True) as d:
            b = d.b
            await asyncio.sleep(5)
            await b.call('Emulation.setCPUThrottlingRate', {'rate': SLOW})
            if '--cold' in sys.argv:
                await b.call('Network.enable'); await b.call('Network.clearBrowserCache'); await b.call('Network.setCacheDisabled', {'cacheDisabled': True})
                await b.js("(async()=>{for(const k of await caches.keys())await caches.delete(k);const r=await navigator.serviceWorker.getRegistrations();for(const x of r)await x.unregister();return true})()")
            await b.call('Profiler.enable')
            await b.call('Profiler.setSamplingInterval', {'interval': 500})
            await b.call('Profiler.start')
            await b.js("window.__oldPage=true;location.reload();true")
            for _ in range(600):
                try:
                    if await b.js("!window.__oldPage && !!(document.getElementById('feed') && document.getElementById('feed').children.length)"):
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            m = await b.js("""(()=>{const t=performance.timing,n=t.navigationStart;const r={};
              r.domInteractive=t.domInteractive-n;r.domContentLoaded=t.domContentLoadedEventEnd-n;r.load=t.loadEventEnd-n;
              r.now=Math.round(performance.now());
              const res=performance.getEntriesByType('resource').filter(e=>/\\.js|\\.css/.test(e.name));
              r.scripts=res.length;r.bytes=res.reduce((s,e)=>s+(e.encodedBodySize||0),0);return r;})()""")
            prof = (await b.call('Profiler.stop'))['profile']
            await b.call('Emulation.setCPUThrottlingRate', {'rate': 1})
            print('CPU slowdown x%g; milestones (ms from navigation): %s' % (SLOW, m))
            nodes = {x['id']: x for x in prof['nodes']}
            dt = prof['timeDeltas']
            self_t = collections.Counter()
            for sid, delta in zip(prof['samples'], dt):
                self_t[sid] += delta
            by_url, by_fn = collections.Counter(), collections.Counter()
            for nid, us in self_t.items():
                cf = nodes[nid]['callFrame']
                url = cf['url'].rsplit('/', 1)[-1].split('?')[0] or ('(' + cf['functionName'] + ')' if cf['functionName'] in ('(program)', '(garbage collector)', '(idle)') else '(native)')
                by_url[url] += us
                by_fn['%s %s:%d' % (cf['functionName'] or '(anon)', url, cf['lineNumber'] + 1)] += us
            total = sum(self_t.values())
            print('profiled %.0f ms of main-thread time' % (total / 1000))
            print('-- by script --')
            for k, v in by_url.most_common(15):
                print('  %7.0f ms  %s' % (v / 1000, k))
            print('-- hottest functions --')
            for k, v in by_fn.most_common(20):
                print('  %7.0f ms  %s' % (v / 1000, k))

asyncio.run(main())
