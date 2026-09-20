"""PCSha256 (static/js/client/sha256.js) is the incremental hasher that lets the client upload files
too large to hold in memory (ISOs) — it hashes the file in slices instead of one arrayBuffer. It is
worthless unless it equals crypto.subtle EXACTLY, so the runtime checks it against subtle across sizes
and every chunk boundary. A wrong hash would make Blossom reject the upload (BUD-01 `x` mismatch)."""
import subprocess
from pathlib import Path


def test_incremental_sha256_matches_subtle_across_sizes_and_chunk_boundaries():
    p = subprocess.run(["node", str(Path(__file__).with_name("test_sha256_stream.mjs"))],
                       text=True, capture_output=True)
    assert p.returncode == 0, (p.stdout + p.stderr)
    assert "0 failures" in p.stdout, p.stdout
