import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_vector_fragmented_membership_runtime():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/concord_vector_membership_runtime.mjs")],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "vector membership runtime ok" in run.stdout


def test_vector_fragment_query_is_not_pinned_to_legacy_empty_d_tag():
    src = (ROOT / "static/js/client/concord.js").read_text()
    assert "query({kinds:[33302],authors:[pubkey],limit:64})" in src
    assert "decodeMembershipLists(decrypted)" in src
