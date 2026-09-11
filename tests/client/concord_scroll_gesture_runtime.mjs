/* A READER'S SCROLL MUST SURVIVE A PROGRAMMATIC RESTORE.
 *
 * Reported as "my position keeps getting reset when I scroll through a room history".
 *
 * `setProgrammaticScroll` marks the scroller while it moves it so `onscroll` does not read our own
 * move as the reader's. That mark used to be the whole test, and it is a TIME WINDOW: every scroll
 * event between setting it and the next animation frame was discarded. A frame is ~16ms and a flick
 * is hundreds of events, and `pinned` — the flag that decides whether the next content growth snaps
 * to the bottom — is computed from exactly those events. So a picture decrypting fires a resize,
 * the resize restores the pin, the flick inside that frame is thrown away, `pinned` stays true, and
 * the next picture snaps the reader back down. In a room full of media that repeats all the way up.
 *
 * WHY THE EXISTING HARNESS COULD NOT SEE IT: `concord_scroll_runtime.mjs` runs
 * `requestAnimationFrame` SYNCHRONOUSLY, so the mark is set and cleared inside one call and the
 * window it is testing is zero-width. This file defers frames the way a browser does — that is the
 * entire difference between a green test and the bug the reader has.
 */
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');

function fn(head){
  const i = source.indexOf(head), begin = source.indexOf('{', i);
  if (i < 0 || begin < 0) throw Error('missing ' + head);
  let depth = 0;
  for (let p = begin; p < source.length; p++){
    if (source[p] === '{') depth++;
    else if (source[p] === '}' && --depth === 0) return source.slice(i, p + 1);
  }
  throw Error('unterminated ' + head);
}

/* THE SHIPPED onscroll RULE, lifted from the `scroller.onscroll=` assignment rather than retyped —
 * a copy here could be corrected independently of the app and would then pass about nothing. */
function shippedOnScroll(){
  const at = source.indexOf('scroller.onscroll=()=>{');
  if (at < 0) throw Error('the Concord onscroll handler moved — re-point this harness');
  const begin = source.indexOf('{', source.indexOf('=>', at));
  let depth = 0;
  for (let p = begin; p < source.length; p++){
    if (source[p] === '{') depth++;
    else if (source[p] === '}' && --depth === 0) return source.slice(begin, p + 1);
  }
  throw Error('unterminated onscroll');
}

export function makeRoom({ height = 2000, viewport = 600, top = 1400, rows: rowCount = 0,
                           rowHeight = 100 } = {}){
  const rafq = [];
  const timers = [];
  const storage = new Map();
  let resizeCb = null;
  let now = 1_000_000;                     // a clock the test moves, so gestures can actually end
  const content = {};
  /* REAL ROWS, because the anchor restore is the half that decides where a reader lands and it is
     addressed entirely through them: `viewportAnchor` finds the first row whose bottom is below the
     fold and remembers its gap, and the correction puts THAT row back at THAT gap. With no rows the
     restore can only answer "I don't know", which is a harness quietly testing the empty case. */
  const rows = [];
  for (let i = 0; i < rowCount; i++){
    rows.push({ dataset: { messageId: 'm' + i }, offsetTop: i * rowHeight, offsetHeight: rowHeight });
  }
  const handlers = {};
  let scrollTop = top;
  let writes = 0;                          // every assignment, including one that changes nothing
  const box = {
    dataset: {}, scrollHeight: height, clientHeight: viewport, isConnected: true,
    querySelector(){ return content; }, querySelectorAll(){ return rows; },
    addEventListener(type, fn){ (handlers[type] = handlers[type] || []).push(fn); },
  };
  /* A BROWSER CLAMPS. `scrollTop = scrollHeight` lands at `scrollHeight - clientHeight`, and a stub
     that stores the raw number instead reports positions no real scroller can hold — which is how a
     fixture comes to disagree with the app about what "at the bottom" means. */
  Object.defineProperty(box, 'scrollTop', {
    get(){ return scrollTop; },
    set(v){ writes++;
            scrollTop = Math.max(0, Math.min(Number(v) || 0,
              Math.max(0, box.scrollHeight - box.clientHeight))); },
  });
  const ctx = {
    window: { requestAnimationFrame: (f) => { rafq.push(f); return rafq.length; } },
    Date: { now: () => now },
    document: { querySelector: (s) => (s === '.cc-messages' ? box : null),
                body: { classList: { contains: () => true } } },
    sessionStorage: { getItem: (k) => storage.get(k) || null,
                      setItem: (k, v) => storage.set(k, String(v)) },
    setTimeout: (f, ms) => { timers.push({ f, at: now + (Number(ms) || 0) }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].f = null; },
    ResizeObserver: class { constructor(cb){ resizeCb = cb; } observe(){} disconnect(){} },
    scrollStates: new Map(), scrollKey: () => 'room:general',
    Number, Math, JSON, String,
  };
  vm.createContext(ctx);
  vm.runInContext([
    fn('function readScroll('), fn('function writeScroll('),
    fn('function setProgrammaticScroll('), fn('function programmaticScrollEvent('),
    fn('function repaintScrollTop('), fn('function viewportAnchor('),
    'let chatGestureUntil=0;', fn('function chatHandOn('), fn('function scrollGesture('),
    'let chatRepaintHold=null;', fn('function whenHandLeaves('), fn('function watchPinnedRoomGrowth('),
    'const scroller = SCROLLER;',
    'globalThis.onscroll_ = () => ' + shippedOnScroll() + ';',
    'globalThis.api = { readScroll, writeScroll, watchPinnedRoomGrowth, setProgrammaticScroll,\n'
    + '                    whenHandLeaves };',
  ].join('\n'), Object.assign(ctx, { SCROLLER: box }));

  const api = ctx.api;
  return {
    box,
    state: () => api.readScroll('room:general'),
    seed: (st) => api.writeScroll('room:general', st),
    watch: () => api.watchPinnedRoomGrowth(box),
    /* A browser runs these on the NEXT frame, never inside the call that scheduled them. */
    frame: () => { rafq.splice(0).forEach((f) => f()); },
    /* Move the clock and fire whatever was due — this is how a gesture ends and how a deferred
       correction gets its chance. */
    wait: (ms) => { now += ms;
      for (const t of timers) { if (t.f && t.at <= now) { const f = t.f; t.f = null; f(); } }
      rafq.splice(0).forEach((f) => f()); },
    /* The reader's hand: the events a finger and a wheel really send. */
    hand: (type) => { (handlers[type] || []).forEach((f) => f({})); },
    grow: (to) => { box.scrollHeight = to; if (resizeCb) resizeCb(); },
    /* Content gaining height ABOVE the reader — a picture decrypting, a link card resolving, a page
       of older history arriving. Every row below it moves down by the same amount. */
    growAbove: (px, from = 0) => {
      rows.forEach((r, i) => { if (i >= from) r.offsetTop += px; });
      box.scrollHeight += px;
      if (resizeCb) resizeCb();
    },
    /* Ask for a repaint the way the live merge does. */
    repaint: (fn) => api.whenHandLeaves(fn),
    rows,
    /* HOW MANY TIMES scrollTop WAS ASSIGNED. The count is the point: a write of the value it
       already holds changes no position and still cancels a momentum scroll, so it is invisible to
       any assertion that only reads the position afterwards. */
    writes: () => writes,
    /* The reader's finger. Returns whether the app heard it. */
    drag: (to) => {
      (handlers.touchstart || []).forEach((f) => f({}));
      box.scrollTop = to;
      const before = JSON.stringify(api.readScroll('room:general'));
      ctx.onscroll_();
      return JSON.stringify(api.readScroll('room:general')) !== before ? 'seen' : 'swallowed';
    },
    /* A scroll event for a move the APP made — must never be mistaken for the reader. */
    echo: () => { const before = JSON.stringify(api.readScroll('room:general'));
                  ctx.onscroll_();
                  return JSON.stringify(api.readScroll('room:general')) !== before ? 'seen' : 'swallowed'; },
  };
}
