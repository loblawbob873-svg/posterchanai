/* Loads every static/js/client module in a vm against a permissive fake page and records the timers each
 * starts during load. Driven by tests/client/test_client_timers_at_load.py. */
const fs = require('fs'), vm = require('vm'), path = require('path');
const dir = process.argv[2];
function fake(name){
  const f = function(){ return fake(name + '()'); };
  return new Proxy(f, { get(t, k){ if(k === Symbol.toPrimitive) return () => ''; if(k === 'then') return undefined; if(k === Symbol.iterator) return function*(){}; return fake(name + '.' + String(k)); },
                        apply(){ return fake(name + '()'); }, construct(){ return fake('new ' + name); }, has(){ return true; }, set(){ return true; } });
}
const out = {};
for(const f of fs.readdirSync(dir).filter(n => n.endsWith('.js')).sort()){
  const timers = [];
  const g = { console: { log(){}, warn(){}, error(){}, info(){}, debug(){} }, Math, JSON, Date, Promise, Object, Array, String, Number, Boolean, Symbol, Map, Set, WeakMap, WeakSet, RegExp, Error, TypeError, Proxy, Reflect, parseInt, parseFloat, isNaN, isFinite, encodeURIComponent, decodeURIComponent, Uint8Array, ArrayBuffer, TextEncoder, TextDecoder, URL, URLSearchParams,
    setTimeout: (fn, ms) => { timers.push(['timeout', Number(ms) || 0]); return 1; },
    setInterval: (fn, ms) => { timers.push(['interval', Number(ms) || 0]); return 1; },
    clearTimeout(){}, clearInterval(){}, requestAnimationFrame(){ return 1; }, queueMicrotask(){} };
  const ctx = new Proxy(g, { get(t, k){ return k in t ? t[k] : fake(String(k)); }, has(){ return true; } });
  g.window = ctx; g.self = ctx; g.globalThis = ctx;
  let err = '';
  try{ vm.runInNewContext(fs.readFileSync(path.join(dir, f), 'utf8'), ctx, { filename: f, timeout: 2000 }); }catch(e){ err = String(e && e.message || e).slice(0, 80); }
  out[f] = { timers, err };
}
console.log(JSON.stringify(out));
