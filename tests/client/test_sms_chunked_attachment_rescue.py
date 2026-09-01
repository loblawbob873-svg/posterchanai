"""AN EMPTY CHUNK IS NOT AN ANSWER — the other half of "the phone answered with no bytes and no reason".

Reported from a handset, with two placeholders on screen at once:

    Photo · not backed up from your phone
    Video · the phone answered with no bytes and no reason

That second sentence already has a fix behind it. `SmsPlugin.attachment`'s `max` means two
different things on the two read paths — "how much of this chunk" when the build chunks, and "the
biggest file you may return" when it does not — and `MmsStore.partBytes` answers NULL rather than a
truncated buffer when a file is over the cap. So asking the older path for a 768 KB maximum turned
every photo above 768 KB into nothing: no bytes, no error, no `tooBig`. That path now asks again for
the whole file.

**The chunked path never got the same rescue.** Its loop ends `if(!q.data){ a = q; break; }` — a
first chunk that comes back empty falls straight into the generic branch and reaches the user as
exactly the reported sentence, on a build that DOES chunk. So the fix covered the older APK and left
the newer one saying the thing it was written to stop saying.

The rescue is deliberately limited to `offset === 0`. A short chunk part way through a transfer is a
different failure, and re-reading the whole file there would throw away everything already
collected — which on a 12 MB video is the difference between a slow read and no read.

The second placeholder is a smaller thing and also wrong: with no parts there is no content type to
read, so "Photo" is a guess, and it is the wrong one for every video, voice note and contact card a
carrier never delivered.

These run the SHIPPED `partData` against a stub plugin — the point is what it ASKS FOR when a read
comes back empty, which no source assertion can see.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SMS = (ROOT / "static/js/client/sms.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

CHUNK = 768 * 1024
WHOLE = 12 * 1024 * 1024


def _slice(start, end):
    a = SMS.index(start)
    b = SMS.index(end, a)
    return SMS[a:b]


HARNESS = r"""
globalThis.window = globalThis;
const CHUNK_BYTES = 768 * 1024, WHOLE_BYTES = 12 * 1024 * 1024;
const ATT_FAIL_RETRY_MS = 15000;
const ATT = new Map();
const attRemember = (id, r) => { if (id) ATT.set(id, Object.assign({ _at: Date.now() }, r)); return r; };
const isImage = ct => /^image\//i.test(String(ct || ''));
const isVideo = ct => /^video\//i.test(String(ct || ''));
const isAudio = ct => /^audio\//i.test(String(ct || ''));
globalThis.PC = { enc: s => String(s) };
globalThis.URL = { createObjectURL: () => 'blob:stub' };

/* Every call the plugin received, and the canned answers, keyed by the `max` asked for. */
globalThis.asked = [];
globalThis.script = [];
const plug = () => ({
  attachment: async (opts) => {
    asked.push({ part: opts.part, offset: opts.offset, max: opts.max });
    const next = script.shift();
    if (next === undefined) return null;
    if (next && next.throw) throw new Error(next.throw);
    return next;
  },
});
%s
process.stdout.write(JSON.stringify({ asked, r: (await partData(%s)) }));
"""


def run(part, script):
    body = _slice("  async function attLabel", "  /* THE ENCRYPTED-STORAGE COPY") \
        if "async function attLabel" in SMS else _slice("  function attLabel(p){", "  function snippetOf(m){")
    b64 = _slice("  async function b64Blob(b64, type){", "  async function partData(p)")
    part_data = _slice("  async function partData(p)", "  function attHtml(p, enc, mi, pi)")
    js = "(async () => {\n" + (HARNESS % (body + b64 + part_data, json.dumps(part))) + "\n})();"
    js = js.replace("globalThis.script = [];", "globalThis.script = %s;" % json.dumps(script))
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "part.mjs"
        f.write_text(js)
        done = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr[-2500:]
        return json.loads(done.stdout)


PART = {"id": 900, "ct": "video/mp4", "name": "clip.mp4"}


def test_a_chunked_read_that_returns_an_empty_first_chunk_asks_for_the_whole_file():
    """THE BUG. The build chunks (it answers with an `offset`), the first chunk carries nothing, and
    before this the read simply gave up and reported no bytes and no reason."""
    got = run(PART, [{"offset": 0, "done": False}, {"data": "AAAA", "total": 4}])
    maxes = [c["max"] for c in got["asked"]]
    assert maxes[0] == CHUNK, "the first read should still be a chunk"
    assert WHOLE in maxes, (
        "an empty first chunk was never retried as a whole file — this is the read that reaches "
        f"the user as 'the phone answered with no bytes and no reason'. Asked: {got['asked']}")
    assert "no bytes and no reason" not in str(got["r"].get("why", ""))


def test_the_rescue_only_fires_at_the_start_of_a_transfer():
    """A short chunk PART WAY THROUGH is a different failure. Re-reading the whole file there throws
    away everything already collected, which on a 12 MB video is the difference between a slow read
    and no read at all."""
    got = run(PART, [
        {"offset": 0, "data": "AAAA", "done": False},   # first chunk arrives
        {"offset": 4, "done": False},                   # then nothing, mid-transfer
    ])
    assert WHOLE not in [c["max"] for c in got["asked"]], (
        "a mid-transfer stall restarted the whole file and discarded the bytes already read")


def test_a_chunk_that_reports_an_error_is_not_second_guessed():
    """The provider's own words are the answer; asking again would replace them with a worse one."""
    got = run(PART, [{"offset": 0, "error": "provider refused attachment", "total": 0}])
    assert WHOLE not in [c["max"] for c in got["asked"]]
    assert "provider refused attachment" in got["r"]["why"]


def test_a_chunk_that_says_the_file_is_too_big_is_not_retried_whole():
    """`tooBig` is a real, useful answer — retrying it whole is a guaranteed second failure and it
    would replace 'open it in your gallery' with something less actionable."""
    got = run(PART, [{"offset": 0, "tooBig": True}])
    assert WHOLE not in [c["max"] for c in got["asked"]]
    assert "gallery" in got["r"]["why"]


def test_the_non_chunking_path_keeps_its_own_rescue():
    """The fix that came first must survive this one: a build with no `offset` in its answer is
    still asked again for the whole file."""
    got = run(PART, [{"data": None}, {"data": "AAAA"}])
    maxes = [c["max"] for c in got["asked"]]
    assert maxes[0] == CHUNK and WHOLE in maxes


def test_an_ordinary_chunked_transfer_is_untouched():
    """The fix must not add a read to the path that already works."""
    got = run(PART, [{"offset": 0, "data": "AAAA", "done": False},
                     {"offset": 4, "data": "BBBB", "done": True}])
    assert WHOLE not in [c["max"] for c in got["asked"]]
    assert got["r"].get("url") or got["r"].get("blob") is not None or not got["r"].get("why"), got["r"]


# --------------------------------------------------------------------------- the wrong label


def test_an_mms_with_no_parts_is_not_called_a_photo():
    """With no parts there is no content type, so naming one is a guess — wrong for every video,
    voice note and contact card the carrier never delivered. The reporter had a video."""
    for claim in ("'Photo \\u00b7 not backed up from your phone'",
                  "'Photo \\u00b7 not backed up'"):
        assert claim not in SMS, f"a media-less MMS is still hardcoded as a photo: {claim}"
    assert "Attachment \\u00b7 not backed up from your phone" in SMS
    assert "Attachment \\u00b7 not backed up" in SMS


def test_the_placeholder_and_the_thread_snippet_agree():
    """Two places say this, and a person sees both at once — the thread list and the open bubble."""
    snippet = _slice("  function snippetOf(m){", "  /* THE ENCRYPTED-STORAGE COPY")
    assert "not backed up" in snippet
    assert "Photo" not in snippet.split("not backed up")[0].split("\n")[-1]
