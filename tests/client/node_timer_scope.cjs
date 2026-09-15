'use strict';

// A completed scenario must not wait for unused Promise.race deadlines. Keep real
// timers during the scenario; dispose only this scope's handles after assertions.
module.exports = function timerScope(target = globalThis) {
  const originalSet = target.setTimeout;
  const originalClear = target.clearTimeout;
  const handles = new Set();
  let closed = false;
  function scopedSet(callback, delay, ...args) {
    if (typeof callback !== 'function') return originalSet(callback, delay, ...args);
    let handle;
    handle = originalSet(function (...values) {
      handles.delete(handle);
      if (!closed) callback.apply(this, values);
    }, delay, ...args);
    handles.add(handle);
    return handle;
  }
  function scopedClear(handle) {
    handles.delete(handle);
    return originalClear(handle);
  }
  target.setTimeout = scopedSet;
  target.clearTimeout = scopedClear;
  return function dispose() {
    if (closed) return;
    closed = true;
    for (const handle of handles) originalClear(handle);
    handles.clear();
    if (target.setTimeout === scopedSet) target.setTimeout = originalSet;
    if (target.clearTimeout === scopedClear) target.clearTimeout = originalClear;
  };
};
