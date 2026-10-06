/* The SHIPPED copyValue on the APK's route (Capacitor Clipboard), against a stub Android clipboard
   that can accept a write and then hold something else -- "it says link copied but nothing pastes". */
import { clientSource } from './client_source.mjs';
const src = clientSource();
const a = src.indexOf('  function copyValue(text, okMsg, failLabel){');
const b = src.indexOf('\n  function _fetchTimeout(', a);
if (a < 0 || b < 0) throw new Error('copyValue moved');
const shipped = src.slice(a, b);

let toasts = [], fallbacks = [], held = '', mode = 'keeps';
globalThis.toast = (m) => { toasts.push(String(m)); };
globalThis.document = { createElement: () => ({ style:{}, setAttribute(){}, focus(){}, select(){}, setSelectionRange(){}, remove(){} }),
                        body: { appendChild(){} }, execCommand: () => true };   // the lying fallback
globalThis._copyFallback = (msg, v) => { fallbacks.push(v); return Promise.resolve(); };
Object.defineProperty(globalThis, 'navigator', { value: {}, configurable: true });
globalThis.window = { Capacitor: { Plugins: { Clipboard: {
  write: async ({ string }) => { if (mode !== 'drops') held = string; },
  read: async () => { if (mode === 'unreadable') throw new Error('denied'); return { type: 'text/plain', value: held }; },
} } } };
const copyValue = new Function(`let _copyCheck=null;\n${shipped}\n; return copyValue;`)();
const out = {};
for (const m of ['keeps', 'drops', 'unreadable']) {
  mode = m; held = 'yesterday'; toasts = []; fallbacks = [];
  const r = await copyValue('https://poster.place/nevent1abc', 'link copied', 'Link to this post:');
  out[m] = { result: r, toasts, fallbacks };
}
process.stdout.write(JSON.stringify(out));
