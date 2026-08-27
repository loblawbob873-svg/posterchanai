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

RESUME_TOP_HARNESS = r"""
const fs = require('fs');
const docListeners = {};
const appListeners = {};
let launchListener;
let parked = '__feed_top';
const events = [];
const document = global.document = {
  visibilityState: 'hidden',
  querySelector: s => s === '#feed' ? {} : null,
  addEventListener: (n, fn) => { (docListeners[n] ||= []).push(fn); }
};
const home = {
  consumeLaunchView: async () => { const v=parked; parked=''; return {view:v}; },
  addListener: (n, fn) => { if(n === 'launchView') launchListener=fn; }
};
const app = { addListener: (n, fn) => { appListeners[n]=fn; } };
global.window = {
  __PC_BOOTED: true,
  __PC: {
    capPlugin: name => name === 'HomeScreen' ? home : (name === 'App' ? app : null),
    timelineTop: () => events.push('top')
  },
  PCOS: { mobileLanding(){} },
  addEventListener(){}
};
global.PCOS = window.PCOS;
global.navigator = {language:'en-US'};
global.location = {origin:'https://example.invalid'};
global.localStorage = {getItem(){return null},setItem(){},removeItem(){}};
eval(fs.readFileSync(process.argv[1], 'utf8'));

setTimeout(() => {
  if(!launchListener || !appListeners.resume) throw new Error('native listeners not installed');
  launchListener({view:'__feed_top'});
  setTimeout(() => {
    if(events.length) throw new Error('scrolled while WebView hidden: '+events.join(','));
    document.visibilityState='visible';
    appListeners.resume();
    setTimeout(() => {
      if(events.join(',') !== 'top') throw new Error('resume did not scroll exactly once: '+events.join(','));
      console.log('ALL OK');
      process.exit(0);
    }, 260);
  }, 250);
}, 20);
"""

DIRECT_TOP_HARNESS = r"""
const fs = require('fs');
let launchListener;
const events = [];
const document = global.document = {
  visibilityState: 'visible',
  querySelector: s => s === '#feed' ? {} : null,
  addEventListener(){}
};
const home = {
  consumeLaunchView: async () => ({view:''}),
  addListener: (name, fn) => { if(name === 'launchView') launchListener=fn; }
};
global.window = {
  __PC_BOOTED: true,
  __PC: {
    capPlugin: name => name === 'HomeScreen' ? home : null,
    timelineTop: () => events.push('top')
  },
  PCOS: { mobileLanding(){} },
  addEventListener(){}
};
global.PCOS = window.PCOS;
global.navigator = {language:'en-US'};
global.location = {origin:'https://example.invalid'};
global.localStorage = {getItem(){return null},setItem(){},removeItem(){}};
eval(fs.readFileSync(process.argv[1], 'utf8'));

setTimeout(() => {
  if(!launchListener) throw new Error('native launch listener not installed');
  // A warm double-Home delivery does not necessarily produce a later App.resume. The direct
  // launchView event must therefore finish the scroll by itself.
  launchListener({view:'__feed_top'});
  setTimeout(() => {
    if(events.join(',') !== 'top') throw new Error('direct launch did not scroll exactly once: '+events.join(','));
    console.log('ALL OK');
    process.exit(0);
  }, 260);
}, 20);
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class TabletLauncherRouting(unittest.TestCase):
    def test_folder_sync_wins_after_slow_boot_and_leaves_desktop(self):
        result = subprocess.run(
            ["node", "-e", HARNESS, str(SRC)], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALL OK", result.stdout)

    def test_double_home_waits_for_webview_resume_before_scrolling(self):
        result = subprocess.run(
            ["node", "-e", RESUME_TOP_HARNESS, str(SRC)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALL OK", result.stdout)

    def test_warm_double_home_scrolls_without_a_resume_callback(self):
        result = subprocess.run(
            ["node", "-e", DIRECT_TOP_HARNESS, str(SRC)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALL OK", result.stdout)
