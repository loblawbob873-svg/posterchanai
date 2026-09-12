"""A fetch that is never answered must not retire the media host for the whole session.

Chromium keeps six sockets PER ORIGIN. A fetch that neither resolves nor is aborted holds one for
the life of the page; six of them and every later request to that origin queues for ever — it does
not fail, so nothing is logged and nothing surfaces. Measured on a real desktop after ~16 minutes of
ordinary use: `poster.place/client/config` answered in 6ms while `media.poster.place/list/<pubkey>`
was still pending at 60,000ms, and firing three more fetches opened NO new socket. A blob that curl
fetched on the same machine in 26ms had taken 303,290ms in the app. Restarting the shell cured it.

The user-visible shape is a dozen unrelated-looking bugs: Files "times out", a folder shows nothing,
an upload that genuinely succeeded never appears, the wallpaper picker reports no pictures.

This drives the SHIPPED guard out of app.js under node against a fake fetch that never settles, and
is written so that removing the guard fails it.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static" / "js" / "client" / "app.js"
SW = ROOT / "static" / "js" / "client" / "sw.js"


def _harness(media_server, cases):
    src = APP.read_text()
    m = re.search(r"\(function boundMediaFetches\(\)\{.*?\n  \}\)\(\);", src, re.S)
    if not m:
        raise AssertionError("boundMediaFetches() not found in app.js — the guard is gone")

    js = """
const location = { href: 'app://posterchan/', origin: 'app://posterchan' };
const mediaServer = () => %s;
const started = [];
let settled = 0;
// A fetch that NEVER settles unless its signal aborts — the exact failure being guarded.
const baseFetch = (input, init) => new Promise((resolve, reject) => {
  const url = typeof input === 'string' ? input : (input && input.url) || '';
  const sig = init && init.signal;
  started.push({url, bounded: !!sig});
  if (sig) sig.addEventListener('abort', () => { settled++; reject(Object.assign(new Error('aborted'), {name:'AbortError'})); });
});
const window = { fetch: baseFetch };
globalThis.window = window;
globalThis.AbortController = AbortController;
globalThis.setTimeout = setTimeout;
globalThis.clearTimeout = clearTimeout;
globalThis.URL = URL;

%s

const cases = %s;
(async () => {
  const out = [];
  for (const c of cases) {
    const p = window.fetch(c.url, c.init || undefined);
    p.catch(()=>{});
    out.push({url: c.url, bounded: started[started.length-1].bounded});
  }
  console.log(JSON.stringify(out));
  // The fakes never settle by design; don't sit out the real 45s ceilings.
  process.exit(0);
})();
""" % (json.dumps(media_server), m.group(0), json.dumps(cases))

    p = subprocess.run(["node", "--input-type=module", "-e", js],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError("node failed: " + (p.stderr or "")[:600])
    return json.loads(p.stdout.strip().splitlines()[-1])


PUBKEY = "4b56bbf41c92e586" + "a" * 48
SHA = "1d510617519c8f6a40d77d71f5e4f0028bd4bfa6090733ec12dd6d3c6fdb26c7"


class StuckMediaFetch(unittest.TestCase):
    def test_every_media_request_is_bounded(self):
        """The regression: the listing and the blob that were measured wedged."""
        got = _harness("https://media.poster.place", [
            {"url": "https://media.poster.place/list/" + PUBKEY},
            {"url": "https://media.poster.place/list/" + PUBKEY + "?limit=50"},
            {"url": "https://media.poster.place/" + SHA},
            {"url": "https://media.poster.place/" + SHA + ".png"},
            {"url": "https://media.poster.place/"},
        ])
        for row in got:
            self.assertTrue(row["bounded"],
                            "an unbounded fetch can still wedge the host: " + row["url"])

    def test_a_blossom_host_we_do_not_own_is_bounded_too(self):
        """A blob read goes to whatever host holds it; the pool damage is identical."""
        got = _harness("https://media.poster.place", [
            {"url": "https://someone-else.example/" + SHA},
            {"url": "https://someone-else.example/blossom/" + SHA},
            {"url": "https://someone-else.example/blossom/list/" + PUBKEY},
        ])
        for row in got:
            self.assertTrue(row["bounded"], "unbounded: " + row["url"])

    def test_a_caller_with_its_own_signal_is_left_alone(self):
        """renderBlossom already aborts at 12s; the guard must not fight an explicit signal."""
        src = APP.read_text()
        self.assertRegex(src, r"if\s*\(hasSignal\s*\|\|\s*!_watched\(url\)\)\s*return\s*_fetch\(",
                         "the guard no longer defers to a caller's own signal")

    def test_unrelated_hosts_are_untouched(self):
        """A ceiling belongs on the pool that was measured wedging, not on every request."""
        got = _harness("https://media.poster.place", [
            {"url": "https://poster.place/client/config"},
            {"url": "app://posterchan/static/js/client/app.js"},
        ])
        for row in got:
            self.assertFalse(row["bounded"],
                             "an unrelated host was given a media timeout: " + row["url"])

    def test_the_service_worker_cache_warmer_is_bounded(self):
        """The warm-up nobody awaits is the worst thing to let hold a socket for ever."""
        src = SW.read_text()
        pump = re.search(r"async function _pumpBlobCache\(\)\{.*?\n\}", src, re.S)
        self.assertIsNotNone(pump, "_pumpBlobCache not found")
        self.assertIn("_cacheWarmSignal()", pump.group(0),
                      "the background blob fetch is unbounded again")
        self.assertIn("function _cacheWarmSignal()", src)


if __name__ == "__main__":
    unittest.main()
