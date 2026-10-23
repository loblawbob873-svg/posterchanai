/* THE LAST HOP OF THE PUSH CHAIN: a notification's route, handed to the SHIPPED phoneshell.js.
 *
 * tests/test_push_chain_server_to_android.py runs the server's real frame through the shipped Java,
 * reads the route the tapped notification parks (`LaunchView.take`), and passes those routes here
 * (PC_ROUTES = [route, ...]). Each one is drained COLD (HomePlugin.consumeLaunchView answers it) and
 * WARM (the native `launchView` announcement) through the real phoneshell.js, against a stub client
 * that records where the app was sent. Prints {route: {cold:[...], warm:[...]}}.
 * The same stub shape as launcher_tile_landing_runtime.mjs — the plugin boundary is what is simulated.
 */
import fs from 'node:fs';
const routes = JSON.parse(process.env.PC_ROUTES || '[]');
const src = fs.readFileSync(new URL('../../static/js/client/phoneshell.js', import.meta.url), 'utf8');

async function land(view, warm){
  const calls = [], listeners = {};
  let parked = view;
  const plugins = {
    HomeScreen: {
      consumeLaunchView: async () => { const v = parked; parked = ''; return { view: v }; },
      addListener: (name, f) => { (listeners[name] = listeners[name] || []).push(f); },
      status: async () => null,
    },
    App: { addListener: () => {} },
  };
  const g = { window: null, localStorage: { getItem: () => null, setItem(){} },
    document: { visibilityState: 'visible', querySelector: s => (s === '#feed' ? {} : null),
                addEventListener(){}, removeEventListener(){} } };
  g.window = g; g.addEventListener = () => {}; g.removeEventListener = () => {};
  g.Capacitor = { Plugins: plugins, isNativePlatform: () => true };
  g.__PC_BOOTED = true;
  g.__PC = {
    capPlugin: (n, m) => { const p = plugins[n]; return p && typeof p[m] === 'function' ? p : null; },
    switchView: v => calls.push(['switchView', v]),
    openThread: id => calls.push(['openThread', id]),
    openMusic: () => calls.push(['openMusic']), timelineTop: () => calls.push(['timelineTop']),
    closeModal(){}, enc: String,
  };
  const fn = new Function('window', 'document', 'localStorage', 'Capacitor', 'setTimeout',
                          'clearTimeout', 'console', 'globalThis_', src + '\nreturn window.PCPhone;');
  const shell = fn.call(g, g, g.document, g.localStorage, g.Capacitor, setTimeout, clearTimeout,
                        { log(){}, warn(){}, error(){} }, g);
  if (warm) { for (const f of (listeners.launchView || [])) await f({ view }); }
  else await shell.consumeLaunchView();
  return calls;
}
const out = {};
for (const r of routes) out[r] = { cold: await land(r, false), warm: await land(r, true) };
console.log(JSON.stringify(out));
process.exit(0);
