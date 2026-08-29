import fs from 'node:fs';

const source = fs.readFileSync(new URL('../../static/js/client/os.js', import.meta.url), 'utf8');
const body = source.slice(source.indexOf('function disposeWindow('), source.indexOf('function closeWin('));
let aiDeletes = 0;
let unmounts = 0;
let disconnects = 0;
let closes = 0;
globalThis.PCTerm = {unmount() { unmounts++; }};
const disposeWindow = Function(
  '_aiContextWins', 'window', 'console', `${body}; return disposeWindow;`,
)({delete() { aiDeletes++; }}, {PCTerm: globalThis.PCTerm}, console);

const windowState = {
  view: 'terminal',
  aiWatch: {disconnect() { disconnects++; }},
  onClose() { closes++; },
};

if (!disposeWindow(windowState)) throw new Error('first disposal was refused');
if (disposeWindow(windowState)) throw new Error('second disposal was not idempotent');
if (aiDeletes !== 1 || disconnects !== 1 || unmounts !== 1 || closes !== 1) {
  throw new Error(`cleanup counts ${aiDeletes}/${disconnects}/${unmounts}/${closes}`);
}
console.log('window dispose runtime: ok');
