"""The private-item carry may only speak when something changes.

The retry fires on EVERY reconnect — and a settings change reconnects — so with one dead relay in
the set it toasted "copying 2 private item(s)… copied 0 of 2" at the user on every visit to
Settings, for ever, about the same standstill. Reported as toast spam, and the number never said
WHICH relay was pinning it at zero.

The shipped `_carryIfRelaysChanged` is RUN with a stubbed carry: same outcome twice → one toast;
progress → a new toast; completion → the flag and the memory both clear.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def _lift():
    src = open(APP, encoding="utf-8").read()
    m = re.search(r"async function _carryIfRelaysChanged\(\)\{[\s\S]*?\n  \}", src)
    assert m, "_carryIfRelaysChanged moved in app.js"
    return m.group(0)


@unittest.skipIf(not NODE, "no node on this node")
class CarryToastTests(unittest.TestCase):
    def _run(self, outcomes):
        js = """
        const toasts = [];
        const store = { pc_carry_flag: '1' };
        global.localStorage = { getItem: k => (k in store ? store[k] : null),
                                setItem: (k, v) => { store[k] = String(v); },
                                removeItem: k => { delete store[k]; } };
        const _CARRY_KEY = 'pc_carry_flag';
        global.toast = m => toasts.push(m);
        global.Relay = { ready: async () => {} };
        const outcomes = %s; let call = 0;
        global.carryPrivateToRelays = async () => outcomes[Math.min(call++, outcomes.length - 1)];
        %s
        (async () => {
          for(let i = 0; i < outcomes.length; i++){
            store[_CARRY_KEY] = store[_CARRY_KEY];   // flag persists unless the run cleared it
            if(!(_CARRY_KEY in store)) break;
            await _carryIfRelaysChanged();
          }
          process.stdout.write(JSON.stringify({ toasts, flag: _CARRY_KEY in store }));
        })();
        """ % (json.dumps(outcomes), _lift())
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_the_same_standstill_is_announced_once(self):
        out = self._run([{"moved": 0, "total": 2, "dead": ["wss://dead.example"]}] * 4)
        self.assertEqual(len(out["toasts"]), 1, out["toasts"])
        self.assertIn("dead.example", out["toasts"][0])
        self.assertIn("fix or remove", out["toasts"][0])
        self.assertTrue(out["flag"], "an incomplete copy cleared the retry flag")

    def test_progress_speaks_again_and_completion_clears_everything(self):
        out = self._run([
            {"moved": 0, "total": 2, "dead": ["wss://dead.example"]},
            {"moved": 0, "total": 2, "dead": ["wss://dead.example"]},
            {"moved": 2, "total": 2, "dead": []},
        ])
        self.assertEqual(len(out["toasts"]), 2, out["toasts"])
        self.assertIn("all your relays", out["toasts"][-1])
        self.assertFalse(out["flag"], "a complete copy left the retry flag armed")

    def test_a_transient_no_answer_says_nothing_and_keeps_trying(self):
        out = self._run([{"busy": True, "moved": 0, "total": 0},
                         {"offline": True, "moved": 0, "total": 0}])
        self.assertEqual(out["toasts"], [])
        self.assertTrue(out["flag"])


if __name__ == "__main__":
    unittest.main()
