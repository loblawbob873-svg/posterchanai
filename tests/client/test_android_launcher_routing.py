"""Run the shipped phone-shell landing path through the slow-tablet boot race."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "static/js/client/phoneshell.js"

HARNESS = r"""
const fs = require('fs');
const listeners = {};
const document = global.document = {
  querySelector: s => s === '#feed' ? {} : null,
  addEventListener: (n, fn, opt) => { (listeners[n] ||= []).push({fn, once:!!(opt&&opt.once)}); },
  dispatchEvent: e => {
    const rows = (listeners[e.type] || []).slice();
    listeners[e.type] = (listeners[e.type] || []).filter(x => !x.once);
    rows.forEach(x => x.fn(e));
  },
  visibilityState: 'visible'
};
global.Event = function(type){ this.type=type; };
const events = [];
let launchListener;
let parked = 'sync';
const home = {
  consumeLaunchView: async () => ({view: parked}),
  addListener: (name, fn) => { if(name === 'launchView') launchListener=fn; }
};
global.window = {
  __PC_BOOTED: false,
  __PC: {
    capPlugin: name => name === 'HomeScreen' ? home : null,
    switchView: v => events.push('view:'+v),
    openMusic: () => events.push('music')
  },
  PCOS: { mobileLanding: () => events.push('exit-desktop') },
  addEventListener(){},
};
global.PCOS = window.PCOS;
global.navigator = {language:'en-US'};
global.localStorage = {getItem(){return null},setItem(){},removeItem(){}};
eval(fs.readFileSync(process.argv[1], 'utf8'));

setTimeout(() => {
  // The old implementation navigated here, before boot's later desktop restore.
  if(events.length) throw new Error('launcher navigated before boot: '+events.join(','));
  events.push('boot-restored-desktop');
  window.__PC_BOOTED = true;
  document.dispatchEvent(new Event('pc-app-ready'));
  setTimeout(() => {
    const got = events.join(',');
    if(got !== 'boot-restored-desktop,exit-desktop,view:sync')
      throw new Error('wrong final route/order: '+got);
    console.log('ALL OK');
    process.exit(0);
  }, 10);
}, 700);
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class TabletLauncherRouting(unittest.TestCase):
    def test_folder_sync_wins_after_slow_boot_and_leaves_desktop(self):
        result = subprocess.run(
            ["node", "-e", HARNESS, str(SRC)], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALL OK", result.stdout)
