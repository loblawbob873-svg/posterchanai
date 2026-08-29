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


def test_the_recovery_pass_is_allowed_to_resolve_an_invite():
    """`localOnly` is truthy on the pass that actually runs: render() calls syncArmadaMemberships
    with 'recovery' whenever a room is already open, which is every ordinary launch for somebody
    who has joined anything — and that pass explicitly asks for `external:true`. Read as local-only
    it skipped the invite hydration, so the empty-bundle repair correctly refused to skip the room
    and then declined to fix it. Measured after the first attempt shipped: bundleChannels was still
    0 on the reporting account."""
    src = (ROOT / "static/js/client/concord.js").read_text()
    assert "if(!url||(localOnly&&!activeRecovery))continue;" in src, (
        "the recovery pass cannot resolve an invite, so a room with an empty bundle can never be "
        "repaired on the path that actually runs"
    )
