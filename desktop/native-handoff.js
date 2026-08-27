'use strict';

/** A two-phase handoff: destination decoration must exist before native compositor state moves. */
async function runAtomicHandoff(ops, timeoutMs = 2000) {
  let timer;
  try {
    const prepared = await Promise.race([
      Promise.resolve().then(() => ops.prepare()),
      new Promise(resolve => { timer = setTimeout(() => resolve(null), timeoutMs); }),
    ]);
    clearTimeout(timer);
    if (!prepared) { await ops.abort(); return false; }
    try { return await ops.commit(prepared); }
    catch (_) { await ops.rollback(); await ops.abort(); return false; }
  } catch (_) {
    clearTimeout(timer);
    await ops.abort();
    return false;
  }
}

module.exports = { runAtomicHandoff };
