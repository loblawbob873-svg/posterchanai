"""A real video, through the real desktop adapter and the SHIPPED chunker, must come back identical.

Run: venv-unified/bin/python -m pytest tests/client/test_video_survives_chunked_sync.py

WHY THIS EXISTS, stated plainly, because the gap it fills was embarrassing. Videos synced back corrupt
while images were fine, and NOT ONE test could see it:

  * two_device_sim.js has its own putParts/getParts. Its copy still carries the exact bug that
    shipped — `if(!plain || !plain.length) throw` — so `a-file-too-big-to-hold-crosses-in-chunks`
    passed throughout, proving only that the simulator agrees with itself.
  * test_sync_short_read.py drives the shipped functions, but only their length arithmetic: crypto
    stubbed, no filesystem, no real file.
  * Nothing at all moved real bytes through the real adapter.

So this is the round trip that was missing. A genuine MP4 goes out through `fsbridge.readPart`, into
the SHIPPED `putParts`, into a stub blob store, back through the SHIPPED `getParts` and
`fsbridge.writePart` + `writeCommit`, and the result is compared to the original **byte for byte**
and re-probed with ffprobe. Only the network and the master key are stubbed; the chunking, the
offsets, the slicing and every disk write are the real ones.

The encryption stub is content-dependent on purpose (an IV derived from the bytes, then a keyed
transform) rather than the identity function. With identity, chunks written in the wrong ORDER or at
the wrong OFFSET can still reassemble to something plausible; with content-derived addressing they
cannot, which is the property the real scheme has and the one the test needs.
"""
import hashlib
import json
import os
import shutil
import subprocess

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")
FSBRIDGE = ROOT / "desktop" / "fsbridge.js"
NODE = shutil.which("node")
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(not (NODE and FFMPEG and FFPROBE),
                                reason="node + ffmpeg/ffprobe are what this measures")

CHUNK = 64 * 1024          # small, so a few-MB clip is genuinely many chunks


def _slice(name, pat):
    import re
    m = re.search(pat, APP, re.S)
    assert m, f"{name} moved — re-point this test"
    return m.group(0)


def _shipped():
    """The real chunker, lifted out of app.js's IIFE."""
    import re
    return "\n".join([
        _slice("_exactPart", r"async _exactPart\([^)]*\)\{.*?\n      \},"),
        _slice("putParts", r"async putParts\([^)]*\)\{.*?\n      \},"),
        _slice("getParts", r"async getParts\([^)]*\)\{.*?\n      \},"),
    ])


HARNESS = r"""
const crypto = require('crypto');
const B = require(%(bridge)s);
B.init({ roots: [{ id: 'r1', dir: %(root)s }], save(){} });

const blobs = new Map();                       // the stub Blossom
const sha256hex = (b) => crypto.createHash('sha256').update(Buffer.from(b)).digest('hex');

/* Content-derived IV + a keyed transform. NOT identity: with identity a chunk written at the wrong
 * offset still reassembles into something, which is exactly the failure being hunted. */
const _contentIV = async (p) => new Uint8Array(crypto.createHash('sha256').update(Buffer.from(p)).digest().subarray(0, 12));
const _masterEncrypt = async (mk, plain, iv) => {
  const out = new Uint8Array(12 + plain.length);
  out.set(iv, 0);
  for(let i = 0; i < plain.length; i++) out[12 + i] = plain[i] ^ iv[i %% 12] ^ 0x5A;
  return out;
};
const _masterDecrypt = (ct) => {
  const iv = ct.subarray(0, 12), body = ct.subarray(12);
  const out = new Uint8Array(body.length);
  for(let i = 0; i < body.length; i++) out[i] = body[i] ^ iv[i %% 12] ^ 0x5A;
  return out;
};
const FilesIdx = { _ensureMK: async () => 'mk' };
const _blobAlreadyStored = async (sha) => blobs.has(sha);
const _shaFromUrl = (u) => String(u).split('/').pop();
const uploadBlob = async (file) => {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const sha = sha256hex(bytes);
  blobs.set(sha, bytes);
  return 'https://blossom.example/' + sha;
};
const _syncBlobBytes = async (sha) => {
  const b = blobs.get(sha);
  if(!b) throw new Error('blob ' + sha.slice(0, 8) + ' unavailable (404)');
  return _masterDecrypt(b);
};

const S = { %(shipped)s };

(async () => {
  const out = {};
  try {
    const size = %(size)d;
    const SHORT_AT = %(short_at)s;      // null, or the offset whose read comes back short
    let readPart = (off, len) => B.readPart('r1', 'in.mp4', off, len);
    if(SHORT_AT !== null){
      const real = readPart;
      readPart = async (off, len) => {
        const b = await real(off, len);
        return off === SHORT_AT ? b.subarray(0, Math.max(1, b.length - 17)) : b;
      };
    }

    const put = await S.putParts(readPart, size, null, %(chunk)d);
    out.chunks = put.chunks.length;
    out.maxBlob = Math.max(...[...blobs.values()].map(b => b.length));

    await S.getParts(put.chunks, (off, bytes) => B.writePart('r1', 'out.mp4', off, bytes), size);
    const st = await B.writeCommit('r1', 'out.mp4', 0);
    out.size = st.size;
    out.ok = true;
  } catch (e) { out.ok = false; out.error = String(e.message || e); }
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture
def clip(tmp_path):
    """A real MP4, big enough to be many chunks."""
    src = tmp_path / "in.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=30:duration=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(src)],
                   check=True, capture_output=True, timeout=180)
    assert src.stat().st_size > 3 * CHUNK, "clip is too small to exercise chunking"
    return src


def _run(tmp_path, size, short_at="null"):
    script = HARNESS % {
        "bridge": json.dumps(str(FSBRIDGE)), "root": json.dumps(str(tmp_path)),
        "shipped": _shipped(), "size": size, "chunk": CHUNK, "short_at": short_at,
    }
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout)


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_a_real_video_round_trips_byte_for_byte(tmp_path, clip):
    """The whole point. Real file, real slicing, real disk writes, shipped chunker."""
    got = _run(tmp_path, clip.stat().st_size)
    assert got["ok"], got
    assert got["chunks"] > 3, f"only {got['chunks']} chunks — not actually exercising chunking"
    assert got["maxBlob"] <= CHUNK + 64, "a blob exceeded one chunk (+IV) — memory ceiling broken"

    out = tmp_path / "out.mp4"
    assert out.exists(), "writeCommit never produced the file"
    assert out.stat().st_size == clip.stat().st_size, "size differs"
    assert _sha(out) == _sha(clip), "THE BYTES CHANGED — this is the corrupted-video bug"


def test_the_round_tripped_video_still_plays(tmp_path, clip):
    """A sha check is the strict test; this is the one that speaks the language the bug was reported
    in — "not even VLC could play them"."""
    got = _run(tmp_path, clip.stat().st_size)
    assert got["ok"], got
    probe = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=nb_frames,codec_name", "-of", "default=nw=1",
                            str(tmp_path / "out.mp4")], capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, "ffprobe cannot read the rebuilt file: " + probe.stderr[-500:]
    assert "codec_name=h264" in probe.stdout, probe.stdout


def test_a_short_read_mid_file_fails_loudly_instead_of_storing_a_holed_video(tmp_path, clip):
    """THE ORIGINAL BUG, injected at the adapter where it really happened. Before the fix this
    returned a perfectly good-looking chunk list whose file was missing 17 bytes in the middle —
    stored, published, and downloaded by every device."""
    got = _run(tmp_path, clip.stat().st_size, short_at=str(CHUNK * 2))
    assert not got["ok"], (
        "a short read was accepted: the stored chunk list is missing bytes and every device would "
        "download a holed video — " + json.dumps(got))
    assert "short read at" in got["error"], got
    assert not (tmp_path / "out.mp4").exists(), (
        "a file was committed from a failed upload — .part must never be renamed into place")


def test_a_missing_chunk_is_refused_rather_than_written_short(tmp_path, clip):
    """The receiving half, for the blobs that are ALREADY bad. getParts must not commit a rebuild
    that does not match the size the manifest recorded."""
    size = clip.stat().st_size
    script = HARNESS % {
        "bridge": json.dumps(str(FSBRIDGE)), "root": json.dumps(str(tmp_path)),
        "shipped": _shipped(), "size": size, "chunk": CHUNK, "short_at": "null",
    }
    # Drop a chunk from the list between put and get — a manifest written by the buggy uploader.
    script = script.replace("await S.getParts(put.chunks,",
                            "put.chunks.splice(1, 1); await S.getParts(put.chunks,")
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout)
    assert not got["ok"], "a short rebuild was committed: " + json.dumps(got)
    assert "refusing to write a damaged file" in got["error"], got


def test_the_simulator_runs_the_shipped_chunker_and_not_a_copy_of_it():
    """The sim used to hand-write putParts/getParts/chunkShas, and its copy kept the pre-fix
    empty-only check — so every chunk scenario in it passed throughout the period videos were coming
    back unplayable. It was agreeing with itself.

    It lifts all three out of app.js now, the same way it already lifted _liveUnder and _blockedBy
    from sync.js. This pins that, because a re-introduced copy would silently restore the blind spot
    while every test stayed green. (Lifting chunkShas matters as much as the other two: it is the
    VERIFY half of the same addressing scheme, and while it hashed plaintext against a putParts that
    hashes ciphertext, every verify disagreed with the upload that produced it.)"""
    import re
    sim = (ROOT / "tests" / "client" / "two_device_sim.js").read_text(encoding="utf-8")
    for fn in ("_exactPart", "chunkShas", "putParts", "getParts"):
        assert ("'%s'" % fn) in sim, f"the sim no longer lifts {fn} from app.js"
    # COMMENTS OUT FIRST. The note in the sim explaining this very bug quotes the old check verbatim,
    # and a checker that reads its own postmortem as a call site cries wolf for ever. (Third time
    # this shape has bitten in this codebase — see test_pc_surface_exists and the _viewChosen guard.)
    code = re.sub(r"/\*.*?\*/", "", sim, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    assert "if(!plain || !plain.length)" not in code, (
        "a hand-written chunker is back in the simulator — that is the blind spot, not the fix")
    assert "readFileSync(" in code and "app.js" in code
