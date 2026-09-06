/* Run the SHIPPED preload memory sampler against a stubbed page. */
import fs from 'node:fs';

const src = fs.readFileSync(new URL('../desktop/preload.js', import.meta.url), 'utf8');
const a = src.indexOf('(() => {\n  const read = () => { try { return performance.memory');
if (a < 0) throw new Error('the memory sampler moved');
const b = src.indexOf('\n})();', a) + '\n})();'.length;
const body = src.slice(a, b);

const mode = process.argv[2];
const sent = [];
const ipcRenderer = { send: (ch, payload) => sent.push({ ch, payload }) };
const performance_ = {
  now: () => 4200,
  memory: mode === 'absent' ? undefined
        : { usedJSHeapSize: 314572800, totalJSHeapSize: 419430400, jsHeapSizeLimit: 2147483648 },
};
const document_ = { getElementsByTagName: () => ({ length: 18432 }) };
const location_ = { pathname: '/index.html', search: '?instance=https://secret.example' };
const timers = [];
const setInterval_ = (fn, ms) => { timers.push(ms); return 0; };

new Function('ipcRenderer', 'performance', 'document', 'location', 'setInterval', body)(
  ipcRenderer, performance_, document_, location_, setInterval_);

process.stdout.write(JSON.stringify({ sent, timers }));
