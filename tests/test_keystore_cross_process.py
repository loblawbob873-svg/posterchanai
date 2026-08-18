"""Three processes share data/keys.json; each must SEE keys the others mint.

The measured disaster: a life-of-the-process cache made the app, the worker and the relay each
blind to storage keys the others created, so each MINTED ITS OWN key for the same user and wrote
it over the file — the relay's gate filled with pubkeys nobody derives (every fresh account's
writes refused "not in web of trust") and one user's documents were sealed under different keys
per process. The cache now revalidates against the file's mtime+size on every read."""
import importlib
import json
import os
import tempfile
import time
import unittest


class CrossProcessKeystore(unittest.TestCase):
    def _fresh(self, keyfile):
        os.environ["POSTERCHANAI_KEYFILE"] = keyfile
        import app.services.keystore as ks
        importlib.reload(ks)
        return ks

    def test_a_key_written_by_another_process_is_seen(self):
        with tempfile.TemporaryDirectory() as d:
            kf = os.path.join(d, "keys.json")
            json.dump({"operator_nsec": "nsec1op", "storage": {"npub1a": "aa" * 32}}, open(kf, "w"))
            ks = self._fresh(kf)
            self.assertEqual(ks.get_storage_seckey("npub1a"), bytes.fromhex("aa" * 32))
            self.assertIsNone(ks.get_storage_seckey("npub1b"))   # cache is now warm and trusted
            # ANOTHER PROCESS adds npub1b (simulated: rewrite the file behind the cache's back)
            time.sleep(0.01)
            json.dump({"operator_nsec": "nsec1op",
                       "storage": {"npub1a": "aa" * 32, "npub1b": "bb" * 32}}, open(kf, "w"))
            self.assertEqual(ks.get_storage_seckey("npub1b"), bytes.fromhex("bb" * 32),
                             "the warm cache is blind to a sibling process's key — this process "
                             "will now MINT a rival key for that user")

    def test_an_unchanged_file_is_not_reread(self):
        with tempfile.TemporaryDirectory() as d:
            kf = os.path.join(d, "keys.json")
            json.dump({"operator_nsec": "nsec1op", "storage": {}}, open(kf, "w"))
            ks = self._fresh(kf)
            ks.get_storage_seckey("npub1a")
            st0 = os.stat(kf).st_mtime_ns
            for _ in range(5):
                ks.get_storage_seckey("npub1a")
            self.assertEqual(os.stat(kf).st_mtime_ns, st0, "reads must not write")


if __name__ == "__main__":
    unittest.main()
