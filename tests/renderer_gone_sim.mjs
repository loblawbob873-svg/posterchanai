/* Run the SHIPPED render-process-gone handler, and the SHIPPED report builders it calls, against a
 * stubbed Electron. Extracting them is deliberate: a copy of this logic in the test would pass
 * while the real handler said "ran out of memory" about a launch failure, which is exactly what it
 * used to do. */
import fs from 'node:fs';
import os from 'node:os';
import nodePath from 'node:path';

const src = fs.readFileSync(new URL('../desktop/main.js', import.meta.url), 'utf8');

function slice(from, toAfter) {
  const a = src.indexOf(from);
  if (a < 0) throw new Error('moved: ' + from);
  const b = src.indexOf(toAfter, a);
  if (b < 0) throw new Error('end moved for: ' + from);
  return src.slice(a, b + toAfter.length);
}

/* The report builders, verbatim. */
const builders = slice('function memoryLine(samples) {', '\n  } catch (_) { return \'\'; }\n}');

const dialogs = [], childDeaths = [];
let reloads = 0;
const dir = fs.mkdtempSync(nodePath.join(os.tmpdir(), 'pc-crash-'));
const dialog = { showErrorBox: (title, body) => dialogs.push({ title, body }) };
const appStub = {
  getPath: () => dir,
  getVersion: () => '1.0.0',
  getAppMetrics: () => [{ pid: 1, type: 'Browser', memory: { workingSetSize: 102400 } }],
};
const built = new Function('app', 'path', 'fs', '_childDeaths',
  builders + '\nreturn { memoryLine, crashReport, writeCrashReport };')(
    appStub, nodePath, fs, childDeaths);

const _memSamples = new Map();
const created = {
  webContents: {
    id: 7,
    on: (name, fn) => { events[name] = fn; },
    reloadIgnoringCache: () => { reloads++; },
  },
};
const events = {};
const loadApp = () => { reloads++; };
const console_ = { warn: () => {} };

const handler = slice('  let _rendererDeaths = [];', '\n  });');
new Function('dialog', 'created', 'loadApp', 'console', '_memSamples', 'memoryLine',
             'writeCrashReport', 'crashReport', handler)(
  dialog, created, loadApp, console_, _memSamples,
  built.memoryLine, built.writeCrashReport, built.crashReport);

const plan = JSON.parse(process.argv[2]);
for (const step of plan) {
  const [reason, exitCode, samples] = step;
  if (samples) _memSamples.set(7, samples);
  events['render-process-gone'](null, { reason, exitCode });
}

let report = '';
try { report = fs.readFileSync(nodePath.join(dir, 'crash-report.txt'), 'utf8'); } catch (_) {}
fs.rmSync(dir, { recursive: true, force: true });
process.stdout.write(JSON.stringify({ dialogs, reloads, report }));
