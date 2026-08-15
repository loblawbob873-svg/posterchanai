"""A chunk is exactly the bytes that were asked for, or the file fails. Never a partial.

Run: venv-unified/bin/python -m pytest tests/client/test_sync_short_read.py

THE BUG THIS EXISTS FOR, in the words it was reported in: "images look ok", videos that not even
VLC would open. That split is the whole diagnosis — only files over one chunk take the chunked
path, so a photo could never be touched and a video could never be missed.

Both filesystem adapters return a SHORT buffer when they cannot fill a request:

    desktop/fsbridge.js   return bytesRead === buf.length ? buf : buf.subarray(0, bytesRead);
    FolderSyncPlugin.java byte[] out = (got == buf.length) ? buf : Arrays.copyOf(buf, got);

and putParts/chunkShas tested only `!plain.length` — catching an EMPTY read and waving a PARTIAL one
straight through. What follows is silent and total: the short piece is encrypted, uploaded and
recorded in the manifest, `off` still advances by a whole chunk, and the bytes in the gap are never
stored by anybody. Every device then downloads the original with holes punched in it. Nothing
raises, nothing logs — from the sweep's point of view every step succeeded.

The receiving half is asserted too, because the two fail independently AND because it is what
protects somebody whose blobs are already bad: getParts sums what it actually wrote and refuses to
commit when it does not match the size the manifest recorded.
"""
import json
import re
import shutil
import subprocess

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")


def _slice(name, pat):
    m = re.search(pat, APP, re.S)
    assert m, f"{name} moved — re-point this test"
    return m.group(0)


def _harness():
    """The shipped _exactPart, putParts loop guard and getParts, with the crypto stubbed out.

    Only the LENGTH arithmetic is under test, so encrypt/decrypt are identity and the 'upload'
    returns the sha it was handed. That keeps the thing being measured — bytes in vs bytes out —
    the only thing that can make it fail."""
    exact = _slice("_exactPart", r"async _exactPart\(readPart, off, want\)\{.*?\n      \},")
    get = _slice("getParts", r"async getParts\(chunks, writePart, expect\)\{.*?\n      \},")
    return "const S = {\n" + exact + "\n" + get + "\n};\n"


pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node runs the shipped code")


def _node(script):
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_a_full_read_is_passed_through_untouched():
    got = _node(_harness() + """
      const reads = [];
      const readPart = async (off, want) => { reads.push([off, want]); return new Uint8Array(want); };
      (async () => {
        const a = await S._exactPart(readPart, 0, 1000);
        console.log(JSON.stringify({ len: a.length, calls: reads.length }));
      })();
    """)
    assert got == {"len": 1000, "calls": 1}, "a good read must not be retried"


def test_a_short_read_is_retried_once_and_then_fails():
    """The pre-fix code accepted this silently. A throw is what turns a corrupted video into a
    reported, retryable failure."""
    got = _node(_harness() + """
      let n = 0;
      const readPart = async (off, want) => { n++; return new Uint8Array(want - 7); };  // always short
      (async () => {
        let err = null;
        try { await S._exactPart(readPart, 4096, 1000); } catch(e){ err = e.message; }
        console.log(JSON.stringify({ err, calls: n }));
      })();
    """)
    assert got["calls"] == 2, "a transient short read deserves exactly one retry"
    assert "short read at 4096" in got["err"] and "wanted 1000" in got["err"] and "got 993" in got["err"], got

    # …and the pre-fix rule, through the same harness, to prove the assertion above measures something.
    old = _node("""
      const readPart = async (off, want) => new Uint8Array(want - 7);
      (async () => {
        const plain = await readPart(0, 1000);
        let err = null;
        if(!plain || !plain.length) err = 'short read at 0';       // the check that used to be there
        console.log(JSON.stringify({ err, accepted: plain.length }));
      })();
    """)
    assert old["err"] is None and old["accepted"] == 993, (
        "the old check would have rejected this, so the new one proves nothing")


def test_a_read_that_recovers_on_the_retry_is_accepted():
    got = _node(_harness() + """
      let n = 0;
      const readPart = async (off, want) => { n++; return new Uint8Array(n === 1 ? want - 1 : want); };
      (async () => {
        const a = await S._exactPart(readPart, 0, 500);
        console.log(JSON.stringify({ len: a.length, calls: n }));
      })();
    """)
    assert got == {"len": 500, "calls": 2}


def test_getparts_refuses_a_chunk_list_that_does_not_rebuild_to_the_recorded_size():
    """This is the half that protects somebody whose stored blobs are ALREADY bad: the next sweep
    declines to write the broken copy rather than handing it to another machine."""
    got = _node(_harness() + """
      global._sizes = { a: 400, b: 400, c: 100 };
      global._syncBlobBytes = async (sha) => new Uint8Array(global._sizes[sha]);
      (async () => {
        const wrote = [];
        const writePart = async (off, bytes) => wrote.push([off, bytes.length]);
        const out = {};
        try { out.total = await S.getParts(['a','b','c'], writePart, 900); }
        catch(e){ out.goodErr = e.message; }
        try { await S.getParts(['a','b','c'], writePart, 1200); }   // a chunk went missing upstream
        catch(e){ out.badErr = e.message; }
        out.wrote = wrote.slice(0, 3);
        console.log(JSON.stringify(out));
      })();
    """)
    assert got["total"] == 900, "a correct rebuild must be accepted"
    assert "goodErr" not in got, got
    assert got["wrote"] == [[0, 400], [400, 400], [800, 100]], got
    assert "rebuilt 900 bytes, expected 1200" in got["badErr"], got
    assert "refusing to write a damaged file" in got["badErr"], got


def test_a_manifest_without_a_size_is_not_blocked():
    """Entries written before `size` was recorded must still download — the check is a guard, not a
    new requirement that would strand old data."""
    got = _node(_harness() + """
      global._syncBlobBytes = async () => new Uint8Array(10);
      (async () => {
        const total = await S.getParts(['x','y'], async () => {}, undefined);
        console.log(JSON.stringify({ total }));
      })();
    """)
    assert got == {"total": 20}


def test_both_upload_paths_go_through_the_guard():
    """chunkShas verifies a file, putParts stores it. A guard on only one of them means the sha
    matches a file the uploader never actually read."""
    for fn in ("chunkShas", "putParts"):
        body = _slice(fn, r"async %s\(readPart, size.*?\n      \}," % fn)
        assert "this._exactPart(readPart" in body, f"{fn} still accepts a partial read"
        assert "!plain.length" not in body, f"{fn} still carries the old empty-only check"
