/* The celebration sweep reads post rectangles inside a FRAME, never straight off its timer.
 *
 * Profiled on a PosterChanOS desktop page (2026-10-08): the 1.5 s sweep read getBoundingClientRect from a
 * setInterval callback, forcing a synchronous layout of the whole page whenever it was dirty -- 95-127 ms per
 * sweep on the page that draws the taskbar and answers the pointer. Runs the SHIPPED sweep code against stub
 * timers and elements that count rectangle reads. */
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict'), path = require('path');
const src = fs.readFileSync(process.env.PC_APP_SOURCE || path.join(__dirname, '../../static/js/client/app.js'), 'utf8');
const a = src.indexOf('  let _celebTick='), b = src.indexOf('  // Tear down every celebration', a);
assert.ok(a > 0 && b > a, 'celebration sweep not found');
let rects = 0, tick = null, frames = [];
const el = () => ({ dataset: { celebrate: 'gm' }, getBoundingClientRect() { rects++; return { height: 40, top: 10, bottom: 50 }; },
  querySelector() { return null; } });
const els = [el(), el(), el()];
const ctx = { console, Date, NO_IMAGES: false, matchMedia: () => ({ matches: false }), window: { innerHeight: 900 },
  document: { hidden: false, documentElement: { clientHeight: 900 } }, $$: () => els,
  setInterval: (f) => { tick = f; return 1; }, clearInterval: () => { tick = null; },
  requestAnimationFrame: (f) => { frames.push(f); return frames.length; },
  _postEffectsOn: () => true, _playCelebration: () => {}, _stopCelebrations: () => {} };
vm.createContext(ctx);
vm.runInContext(src.slice(a, b) + ';globalThis.observeCelebrations=observeCelebrations;', ctx);
ctx.observeCelebrations();
assert.ok(tick, 'no sweep timer was started');
tick();
assert.equal(rects, 0, 'the timer read layout itself -- a forced synchronous layout every 1.5 s');
assert.equal(frames.length, 1, 'the sweep was not handed to a frame');
tick(); assert.equal(frames.length, 1, 'a second tick queued a second frame before the first ran');
frames.shift()();
assert.equal(rects, els.length, 'the frame did not sweep');
ctx.document.hidden = true; tick();
assert.equal(frames.length, 0, 'a hidden page still scheduled a sweep');
console.log('celebration sweep: layout read inside a frame, never off the timer');
