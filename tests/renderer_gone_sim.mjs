/* Run the SHIPPED render-process-gone handler against a stubbed Electron. */
import fs from 'node:fs';
const src = fs.readFileSync(new URL('../desktop/main.js', import.meta.url), 'utf8');
const a = src.indexOf("  let _rendererDeaths = [];");
const b = src.indexOf("  });", src.indexOf("created.webContents.on('render-process-gone'", a)) + 5;
if (a < 0) throw new Error('render-process-gone handler moved');
const body = src.slice(a, b);

const dialogs = [], events = {};
let reloads = 0;
const dialog = { showErrorBox: (title, body) => dialogs.push({ title, body }) };
const created = { webContents: { on: (name, fn) => { events[name] = fn; },
                                reloadIgnoringCache: () => { reloads++; } } };
const loadApp = () => { reloads++; };
const console_ = { warn: () => {} };
new Function('dialog', 'created', 'loadApp', 'console', body)(dialog, created, loadApp, console_);

for (const [reason, exitCode] of JSON.parse(process.argv[2])) {
  events['render-process-gone'](null, { reason, exitCode });
}
process.stdout.write(JSON.stringify({ dialogs, reloads }));
