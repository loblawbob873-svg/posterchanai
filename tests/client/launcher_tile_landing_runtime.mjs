/* EVERY LAUNCHER TILE, DRIVEN THROUGH THE SHIPPED HANDOFF, LANDS ON ITS OWN SCREEN.
 *
 * The coverage that existed sat on BOTH SIDES of this seam and never crossed it:
 *   * `AppViewsLaunchSmokeTest` opens every view — by calling `__PC.switchView(view)` in JS. It
 *     never goes near a launcher intent.
 *   * `test_android_launch_view.py::test_the_catalogue_matches_the_sidebar` checks that every tile
 *     NAMES a view the client has. A tile can name a perfectly valid view and still land nowhere.
 *   * `LauncherDeviceTest` walks one tile, and that tile is Texts — a native Activity, not a view.
 *
 * So nothing walked tile → parked request → HomePlugin → client → the screen, which is exactly the
 * seam "Messages doesn't load from the launcher" fell through.
 *
 * This runs the SHIPPED phoneshell.js once per tile, against a stub of the plugin surface, and
 * asserts where each one lands. The tile list is passed in from the shipped `HomeTiles.java`
 * catalogue — never typed here — so a tile added later joins this gate by itself.
 *
 * WHICH HALF IS SIMULATED: the plugin boundary. `LaunchView` (the park/take/staleness/delivery
 * rules) is RUN under java by tests/test_android_launch_view.py, `HomePlugin.consumeLaunchView`'s
 * read order is asserted there too, and `LauncherTileLandsOnItsScreenDeviceTest` drives the whole
 * chain on a real device — which cannot run on this box (no KVM), so the assertions that can run
 * here are the ones carrying the rule.
 */
import fs from 'node:fs';import assert from 'node:assert/strict';

const tiles = JSON.parse(process.env.PC_TILES || '[]');
assert(tiles.length >= 20, 'the tile catalogue came through empty or tiny: ' + tiles.length);

const src = fs.readFileSync(process.env.PC_PHONESHELL_SOURCE
  || new URL('../../static/js/client/phoneshell.js', import.meta.url), 'utf8');

/** Boot the shipped module against a stub client + plugin, hand it one parked view, and report. */
async function land(view, { warm }){
  const calls = [];
  const listeners = {};
  let parked = view;
  const plugins = {
    HomeScreen: {
      consumeLaunchView: async () => { const v = parked; parked = ''; return { view: v }; },
      addListener: (name, f) => { (listeners[name] = listeners[name] || []).push(f); },
      status: async () => null,
    },
    App: { addListener: () => {} },
  };
  const g = {
    window: null,
    localStorage: { getItem: () => null, setItem(){} },
    document: {
      visibilityState: 'visible',
      querySelector: s => (s === '#feed' ? {} : null),
      addEventListener(){}, removeEventListener(){},
    },
  };
  g.window = g;
  g.addEventListener = () => {}; g.removeEventListener = () => {};
  g.Capacitor = { Plugins: plugins, isNativePlatform: () => true };
  g.__PC_BOOTED = true;
  g.__PC = {
    capPlugin: (n, m) => { const p = plugins[n]; return p && typeof p[m] === 'function' ? p : null; },
    switchView: v => calls.push(['switchView', v]),
    openThread: id => calls.push(['openThread', id]),
    openMusic: () => calls.push(['openMusic']),
    timelineTop: () => calls.push(['timelineTop']),
    closeModal(){}, enc: String,
  };
  // The module is an IIFE that reads free globals; run it with `g` AS the global object.
  const fn = new Function('window', 'document', 'localStorage', 'Capacitor', 'setTimeout',
                          'clearTimeout', 'console', 'globalThis_', src + '\nreturn window.PCPhone;');
  const shell = fn.call(g, g, g.document, g.localStorage, g.Capacitor, setTimeout, clearTimeout,
                        { log(){}, warn(){}, error(){} }, g);
  assert(shell && typeof shell.consumeLaunchView === 'function',
         'phoneshell.js no longer publishes PCPhone — re-read this test');
  if (warm) {
    // The warm press: MainActivity.onNewIntent announces the view and the client performs it.
    const fired = listeners.launchView || [];
    assert.equal(fired.length, 1, 'nothing subscribed to the native launchView event');
    await fired[0]({ view });
  } else {
    // The cold press: nothing announced anything, the parked request is drained on arrival.
    await shell.consumeLaunchView();
  }
  return calls;
}

/* app.js's own special destinations. A tile view is an ordinary slug, so none of these should ever
 * be reached from the catalogue — they are listed so a future tile that IS one of them fails
 * loudly here rather than landing on the default screen in somebody's hand. */
const SPECIAL = new Set(['__feed_top', '__music']);

for (const view of tiles) {
  assert(!SPECIAL.has(view), 'tile ' + view + ' collides with a phoneshell special destination');
  for (const warm of [false, true]) {
    const how = warm ? 'warm (onNewIntent)' : 'cold (parked)';
    const calls = await land(view, { warm });
    assert.equal(calls.length, 1,
      'tile "' + view + '" ' + how + ' produced ' + calls.length + ' navigations: '
      + JSON.stringify(calls));
    assert.deepEqual([calls[0][0], calls[0][1]], ['switchView', view],
      'tile "' + view + '" ' + how + ' landed on ' + JSON.stringify(calls[0])
      + ' — the app comes forward on the wrong screen, which is indistinguishable from a tile '
      + 'wired to the wrong slug');
  }
}

console.log('launcher tiles: ' + tiles.length + ' tiles reach their own view, cold and warm');
process.exit(0);
