"""The retry fixture must model async timers, including delayed query responses."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_boundaries_cancellation_and_async_timers():
    clock = Path(__file__).with_name('virtual_timeout_clock.js').read_text()
    script = '''
(async () => {
  const events = [];
  const cancelled = setTimeout(() => events.push('cancelled'), 5);
  clearTimeout(cancelled);
  setTimeout(async () => {
    events.push(['start', clockNow]);
    await Promise.resolve();
    await Promise.resolve();
    await new Promise(r => setTimeout(r, 300));
    events.push(['answer', clockNow]);
    setTimeout(() => events.push(['retry', clockNow]), 900);
  }, 150);
  setTimeout(() => events.push(['same deadline', clockNow]), 150);
  await advance(149);
  if (events.length) throw new Error('timer ran early');
  await advance(1);
  const first = events.slice();
  await advance(1199);
  const beforeRetry = events.slice();
  await advance(1);
  console.log(JSON.stringify({first, beforeRetry, events, pending: clockTimers.size}));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run(['node', '-e', clock + script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    first = [['start', 150], ['same deadline', 150]]
    assert got['first'] == first
    assert got['beforeRetry'] == first + [['answer', 450]]
    assert got['events'] == first + [['answer', 450], ['retry', 1350]]
    assert got['pending'] == 0
