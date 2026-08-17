"""The Android folder listing crosses the bridge in PAGES, driven at the size that broke it.

WHY THIS EXISTS. `scan()` answered with every file in the folder in one reply. At the size of a real
Pictures folder — measured at about 15,000 files — that listing exists FOUR TIMES SIMULTANEOUSLY at
the moment it crosses: the Java map, the org.json copy, the multi-megabyte JSON string Capacitor
serialises to move it, and the parsed object in the WebView. A WebView has far less headroom than the
desktop this engine was written on, and the result was the RENDER PROCESS being killed the instant a
sweep of that folder started.

That failure is invisible to every instrument: no exception is thrown in this process, nothing
reaches logcat, and the app stays in the recents list because the process itself never died — only
its renderer. It was reported as "as soon as pictures starts syncing, it closes", and before that as
"the app closes but still open? i see it in the windows list", which is the sentence that finally
identified it.

The same reasoning already produced readPart/writePart for file CONTENT. The directory listing was
the last whole-folder object left.

This runs the SHIPPED shim against a stub plugin at that size and asserts on the two things that
matter: nothing is lost, and nothing crosses in one piece.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHIM = os.path.join(ROOT, "static", "js", "client", "fs-android.js")

# The size that actually broke a phone.
TOTAL = 15790

DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const TOTAL = %d, PAGE_SEEN = [];
const files = {};
for (let i = 0; i < TOTAL; i++) files['p/photo' + i + '.jpg'] = { size: i, mtime: i };
const keys = Object.keys(files);
global.window = {
  Capacitor: { isNativePlatform: () => true, Plugins: { FolderSync: {
    scan: async (o) => {
      const off = o.offset | 0, lim = o.limit | 0;
      const end = lim <= 0 ? keys.length : Math.min(keys.length, off + lim);
      PAGE_SEEN.push(end - off);
      const out = {};
      for (let i = off; i < end; i++) out[keys[i]] = files[keys[i]];
      return { files: out, skipped: [{ rel: 'x', why: 'too big' }], total: keys.length,
               done: end >= keys.length };
    },
  } } },
};
global.navigator = { userAgent: 'android' };
new Function('window', 'navigator', src)(global.window, global.navigator);
(async () => {
  const r = await global.window.pcFs.scan('tree://x', { hash: false });
  console.log(JSON.stringify({
    files: Object.keys(r.files).length,
    pages: PAGE_SEEN.length,
    biggest: Math.max.apply(null, PAGE_SEEN),
    skipped: (r.skipped || []).length,
    firstKey: Object.keys(r.files)[0],
    lastKey: Object.keys(r.files)[Object.keys(r.files).length - 1],
  }));
})();
""" % TOTAL

# A plugin from before paging ignores `limit` and omits `done`. The shim must take that reply whole
# rather than looping for ever asking for a page two that will never come.
DRIVER_OLD_APK = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const files = {}; for (let i = 0; i < 40; i++) files['p/' + i] = { size: i };
let calls = 0;
global.window = {
  Capacitor: { isNativePlatform: () => true, Plugins: { FolderSync: {
    scan: async () => { calls++; return { files, skipped: [] }; },   // no total, no done
  } } },
};
global.navigator = { userAgent: 'android' };
new Function('window', 'navigator', src)(global.window, global.navigator);
(async () => {
  const r = await global.window.pcFs.scan('tree://x', {});
  console.log(JSON.stringify({ files: Object.keys(r.files).length, calls }));
})();
"""


def _node(driver, tmp_path):
    if shutil.which("node") is None:
        pytest.skip("no node")
    p = tmp_path / "drv.js"
    p.write_text(driver, encoding="utf-8")
    r = subprocess.run(["node", str(p), SHIM], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_real_pictures_folder_never_crosses_the_bridge_in_one_piece(tmp_path):
    got = _node(DRIVER, tmp_path)

    assert got["files"] == TOTAL, "files were lost in the paging — worse than the bug it fixes"
    assert got["biggest"] <= 2000, (
        "the whole folder still crosses in one reply: at this size that is four simultaneous copies "
        "and a dead render process the moment a sweep starts"
    )
    assert got["pages"] > 1, "it did not actually page"
    # Order has to survive, since the diff walks these and a manifest is keyed on the path.
    assert got["firstKey"] == "p/photo0.jpg"
    assert got["lastKey"] == "p/photo%d.jpg" % (TOTAL - 1)
    # Skipped files come back ONCE, not once per page.
    assert got["skipped"] == 1


def test_an_apk_older_than_paging_still_works(tmp_path):
    """The shim ships with the web client and can meet an APK built before the plugin could page. A
    reply with no `done` is a whole listing, not the first page of one."""
    got = _node(DRIVER_OLD_APK, tmp_path)
    assert got["calls"] == 1, "it kept asking for pages an older plugin will never send"
    assert got["files"] == 40


def test_a_file_is_never_held_whole_above_the_platforms_own_chunk_size():
    """The whole-file upload path holds the plaintext, the base64 crossing the bridge, the ciphertext
    and the upload body AT ONCE — three to four times the file. `chunkAbove` is what decides which
    files take it, and it was hardcoded to the DESKTOP's 16 MB chunk while Android's is 4 MB. Every
    photo and video between the two therefore went the expensive way on the device with the least
    headroom, which is the renderer dying mid-sweep.

    Structural, deliberately: this is about the value handed to the executor, and the executor's own
    behaviour at that value is already covered by tests/client/test_sync_run.py."""
    import re
    src = open(os.path.join(ROOT, "static", "js", "client", "sync.js"), encoding="utf-8").read()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    m = re.search(r"chunkAbove:\s*([^,\n]+)", src)
    assert m, "the sweep no longer passes chunkAbove at all"
    expr = m.group(1).strip()
    assert "chunkSize()" in expr, (
        "chunkAbove is a fixed number again (%s) — on any platform whose chunk is smaller, every "
        "file in between is held whole on the device least able to afford it" % expr
    )
