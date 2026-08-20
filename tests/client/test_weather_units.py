"""The weather widget uses the scale the person's own region uses, and can be told otherwise.

    "weather widget is in Celcius, i am in the US"

`pc_units` was READ in exactly one place and WRITTEN by nothing, anywhere in the codebase. So the
fallback beside the read was the answer for every person on every device — a hardcoded 'metric',
with no switch and no way to reach it. A default that cannot be changed is not a default, it is a
decision made on somebody else's behalf.

The device already knows: the resolved locale carries the region the phone is actually set to, and
the set of places that report weather in Fahrenheit is small, closed and easy to name. So the
default is derived, the stored preference always wins over it, and there is a switch.

REGION, NEVER LANGUAGE — `en` is spoken in Britain, Australia and India, all of which use Celsius,
so guessing from the language would put most English speakers on the wrong scale.

Run: venv-unified/bin/python -m unittest tests.client.test_weather_units
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "static" / "js" / "client" / "phoneshell.js"

# The shipped module, driven under node with a stubbed locale and localStorage. No DOM: renderSettings
# is never called, and nothing else here touches `document`.
HARNESS = r"""
const fs = require('fs'), path = require('path');
const opt = JSON.parse(process.argv[2] || '{}');

const store = Object.assign({}, opt.stored || {});
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
global.navigator = { language: opt.language || 'en-US' };
global.Intl = { DateTimeFormat: () => ({ resolvedOptions: () => ({ locale: opt.locale }) }) };
if (opt.locale === null) {
  // A runtime that cannot answer at all — the branch that must fall back rather than throw.
  global.Intl = { DateTimeFormat: () => { throw new Error('no Intl'); } };
}

const synced = [];
global.window = {
  __PC: {
    capPlugin: (name, method) => {
      if (name !== 'Weather') return null;
      return { sync: async (a) => { synced.push(a); } };
    },
  },
};
global.document = { addEventListener(){}, querySelector(){ return null; } };
global.setTimeout = setTimeout;

eval(fs.readFileSync(opt.src, 'utf8'));

setTimeout(() => {
  const P = global.window.PCPhone;
  const out = { region: P.regionOf(), units: P.unitsPref() };
  if (opt.set) out.after = P.setUnits(opt.set);
  setTimeout(() => {
    out.stored = store.pc_units || null;
    out.synced = synced.map(s => s && s.units);
    console.log(JSON.stringify(out));
    // EXIT DELIBERATELY. phoneshell's init() retries on a timer until `window.__PC` appears, and a
    // pending timer keeps node's event loop alive for ever — the harness would hang rather than fail.
    process.exit(0);
  }, 30);
}, 80);
"""


def run(**opts):
    import tempfile, os
    with tempfile.TemporaryDirectory(dir=str(ROOT / "tests" / "client")) as d:
        h = Path(d) / "h.js"
        h.write_text(HARNESS)
        opts = dict(opts, src=str(SRC))
        r = subprocess.run(["node", str(h), json.dumps(opts)], capture_output=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(r.stderr.decode()[-2000:])
        return json.loads(r.stdout.decode().strip().splitlines()[-1])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Units(unittest.TestCase):

    def test_a_us_phone_gets_fahrenheit(self):
        """The report, in one line."""
        r = run(locale="en-US")
        self.assertEqual(r["region"], "US")
        self.assertEqual(r["units"], "imperial")

    def test_everywhere_else_keeps_celsius(self):
        for loc in ("en-GB", "en-AU", "en-IN", "fr-FR", "ja-JP", "de-DE", "pt-BR"):
            with self.subTest(locale=loc):
                self.assertEqual(run(locale=loc)["units"], "metric")

    def test_the_language_alone_never_decides(self):
        """`en` is Britain, Australia and India as much as it is the US. Guessing from the language
        would put most English speakers on the wrong scale."""
        self.assertEqual(run(locale="en")["units"], "metric")
        self.assertEqual(run(locale="en")["region"], "")

    def test_a_region_buried_in_a_longer_tag_is_still_found(self):
        """`en-Latn-US` and `und-US-u-ca-gregory` are both things a real runtime returns."""
        self.assertEqual(run(locale="en-Latn-US")["units"], "imperial")
        self.assertEqual(run(locale="und-US-u-ca-gregory")["units"], "imperial")

    def test_a_stored_choice_always_wins(self):
        """A person who has chosen is never re-guessed at — in either direction."""
        self.assertEqual(run(locale="en-US", stored={"pc_units": "metric"})["units"], "metric")
        self.assertEqual(run(locale="en-GB", stored={"pc_units": "imperial"})["units"], "imperial")

    def test_junk_in_storage_falls_back_to_the_region(self):
        self.assertEqual(run(locale="en-US", stored={"pc_units": "banana"})["units"], "imperial")

    def test_a_runtime_that_cannot_say_does_not_throw(self):
        r = run(locale=None)
        self.assertEqual(r["region"], "")
        self.assertEqual(r["units"], "metric")

    def test_choosing_stores_it_and_tells_the_widget_at_once(self):
        """Stored only is not enough: the widget would keep the old scale until the next hourly
        tick, which is a switch that appears to do nothing for an hour."""
        r = run(locale="en-GB", set="imperial")
        self.assertEqual(r["after"], "imperial")
        self.assertEqual(r["stored"], "imperial")
        self.assertIn("imperial", r["synced"])
