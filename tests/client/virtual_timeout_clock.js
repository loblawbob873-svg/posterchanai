// Test-local timeout clock. Run promise continuations between timer callbacks, just as Node does.
// A macrotask boundary drains the full microtask queue, including ready() -> query() -> retry chains.
let clockNow = 0;
let nextClockId = 1;
const clockTimers = new Map();
const setTimeout = (callback, delay = 0, ...args) => {
  const id = nextClockId++;
  clockTimers.set(id, { at: clockNow + Math.max(1, Number(delay) || 0), callback, args });
  return id;
};
const clearTimeout = id => clockTimers.delete(id);
const drainPromises = () => new Promise(resolve => setImmediate(resolve));
const advance = async ms => {
  if (!Number.isFinite(ms) || ms < 0) throw new Error('invalid clock advance');
  const end = clockNow + ms;
  await drainPromises();
  for (let steps = 0; ; steps++) {
    const due = [...clockTimers.entries()].filter(([, timer]) => timer.at <= end)
      .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
    if (!due) break;
    if (steps >= 10000) throw new Error('virtual timer loop exceeded 10000 callbacks');
    const [id, timer] = due;
    clockTimers.delete(id);
    clockNow = timer.at;
    timer.callback(...timer.args);
    await drainPromises();
  }
  clockNow = end;
};
