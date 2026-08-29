'use strict';

const file = process.env.PC_INSTALLED_CLIPBOARD_JS;
if (!file) throw new Error('PC_INSTALLED_CLIPBOARD_JS is required');
process.env.WAYLAND_DISPLAY = process.env.WAYLAND_DISPLAY || 'wayland-installed-gate';
const C = require(file);

let pipeError = null;
const child = {
  stdin: {
    once(name, fn) { if (name === 'error') pipeError = fn; },
    end() { if (typeof pipeError === 'function') queueMicrotask(() => pipeError(new Error('EPIPE'))); },
  },
  once(name, fn) { this[name] = fn; },
  kill() {},
};

const checked = C.writeWaylandText('installed-native-copy', { spawn: () => child });
if (typeof pipeError !== 'function') {
  console.error('installed clipboard lacks the bounded Wayland stdin error listener');
  process.exitCode = 1;
} else checked.then(ok => {
  if (ok !== false) throw new Error('installed clipboard accepted a broken Wayland pipe');
  process.stdout.write('OK installed clipboard contains the bounded Wayland failure path\n');
}).catch(err => {
  console.error(err);
  process.exitCode = 1;
});
