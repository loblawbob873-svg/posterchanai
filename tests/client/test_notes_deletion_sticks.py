"""A deleted note must stay deleted — `_absorb`, run against real event batches.

THE BUG, and it was not a rare race. A deletion is an empty-content tombstone, and `_absorb`
applied it with `into.delete(id)`. That threw away the only thing that could refuse the older copy:
the "already have something newer" guard protects what is still IN the map, and after a delete there
is nothing there to protect it.

Events are sorted NEWEST FIRST, so within a single batch the tombstone is applied first (deleting
nothing, because the map is empty) and the real note that follows finds no entry and is put straight
back. The client's Store keeps both events, so this repeated on every load: the note was deleted,
and it was there again.

Run under node against the shipped notes.js, with the module's dependencies stubbed — the point is
the absorb table, and it is pure once decryption is stubbed.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "static", "js", "client", "notes.js")
NODE = shutil.which("node") or shutil.which("nodejs")

HARNESS = r"""
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.setTimeout = setTimeout; globalThis.clearTimeout = clearTimeout;
globalThis.document = { addEventListener(){}, querySelector: () => null,
                        querySelectorAll: () => [], createElement: () => ({ style:{} }) };
globalThis.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
globalThis.__PC = globalThis.PC = {
  ME: { pubkey: 'me' }, me: () => ({ pubkey: 'me' }),
  VIEW: 'notes', $: () => null, $$: () => [], enc: s => String(s), toast(){},
  // The ciphertext IS the plaintext here: this test is about the absorb table, not NIP-44.
  nip44dec: async (_pk, ct) => ct,
  nip44enc: async (_pk, s) => s,
};
globalThis.Relay = () => ({ query: async () => [], subscribe: null });
globalThis.Store = () => ({ query: () => [] });
require(%s);
const N = globalThis.PCNotes;
"""


@unittest.skipIf(not NODE, "no node on this node")
class DeletionSticks(unittest.TestCase):
    def absorb(self, events):
        """Run the shipped _absorb over a batch and report which notes survive."""
        js = (HARNESS % json.dumps(MOD)) + """
        (async () => {
          const lib = { notes: new Map(), folders: new Map(), gone: new Map() };
          await N._absorb(lib, %s);
          process.stdout.write(JSON.stringify({ notes: [...lib.notes.keys()] }));
          // notes.js registers timers and a flush handler when it loads, so the event loop never
          // drains on its own and the harness would hang rather than fail.
          process.exit(0);
        })();
        """ % json.dumps(events)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=40)
        self.assertEqual(r.returncode, 0, r.stderr[-1200:])
        return json.loads(r.stdout)

    @staticmethod
    def ev(nid, at, content):
        return {"created_at": at, "content": content, "tags": [["d", "pcai:note:" + nid]]}

    def test_a_tombstone_beats_an_older_copy_in_the_same_batch(self):
        """The exact shape that shipped: both events present, newest first."""
        out = self.absorb([self.ev("a", 100, json.dumps({"title": "gone"})),
                           self.ev("a", 200, "")])
        self.assertEqual(out["notes"], [],
                         "the deleted note came back — the tombstone was applied and then the "
                         "older copy walked over it")

    def test_it_still_holds_when_the_older_copy_arrives_last(self):
        """A lagging relay serves them in the other order; the answer must not depend on arrival."""
        out = self.absorb([self.ev("a", 200, ""),
                           self.ev("a", 100, json.dumps({"title": "gone"}))])
        self.assertEqual(out["notes"], [])

    def test_a_restore_still_wins(self):
        """Undeleting is a NEWER event than the tombstone, and must land. A guard that refused
        everything after a delete would make deletion permanent, which is the same bug inverted."""
        out = self.absorb([self.ev("a", 100, json.dumps({"title": "one"})),
                           self.ev("a", 200, ""),
                           self.ev("a", 300, json.dumps({"title": "back"}))])
        self.assertEqual(out["notes"], ["a"])

    def test_other_notes_are_untouched(self):
        out = self.absorb([self.ev("a", 100, json.dumps({"title": "one"})),
                           self.ev("a", 200, ""),
                           self.ev("b", 150, json.dumps({"title": "two"}))])
        self.assertEqual(out["notes"], ["b"])


if __name__ == "__main__":
    unittest.main()
