"""A Zapstore outage of a few minutes must not cost a release.

2026-10-10: 1.0.2505 shipped to GitHub Releases and never reached Zapstore -- relay.zapstore.dev and
cdn.zapstore.dev timed out during the one attempt the job made ("zap store never updated"). The publish is
idempotent, so the step retries with backoff. This RUNS the step's own block (taken from the workflow, so a
copy cannot drift) under bash with a stub `zsp` and a no-op `sleep`.
"""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _block():
    wf = (ROOT / ".github/workflows/android.yml").read_text()
    m = re.search(r"\n( *)published=''\n.*?\n\1fi\n", wf, re.S)
    assert m, "the retry block is gone from the Zapstore step"
    return "\n".join(l[len(m.group(1)):] for l in m.group(0).strip("\n").splitlines())


def _run(tmp_path, outcomes):
    """outcomes: per attempt, 'ok', 'fail' (zsp exits 1) or 'reject' (exits 0, AppCatalog refused the asset)."""
    state = tmp_path / "n"
    state.write_text("0")
    zsp = tmp_path / "zsp"
    zsp.write_text("#!/bin/bash\nn=$(cat %s); echo $((n+1)) > %s\noutcomes=(%s)\ncase ${outcomes[$n]} in\n"
                   "  ok) echo 'software_asset -> wss://relay.zapstore.dev: ok'; exit 0;;\n"
                   "  reject) echo 'software_asset -> wss://relay.zapstore.dev: FAILED (rejected)'; exit 0;;\n"
                   "  *) echo 'Error: failed to upload file: connection timed out'; exit 1;;\nesac\n"
                   % (state, state, " ".join(outcomes)))
    zsp.chmod(0o755)
    verify = tmp_path / "verify"          # stands in for scripts/zapstore_verify.py: held unless told otherwise
    verify.write_text("#!/bin/bash\n[ -f %s ] && exit 1; exit 0\n" % (tmp_path / "not_held"))
    verify.chmod(0o755)
    block = _block().replace("/tmp/zsp-publish.log", str(tmp_path / "log")).replace("/tmp/zsp", str(zsp))
    block = block.replace("python3 scripts/zapstore_verify.py", str(verify)).replace("${{ github.run_number }}", "2505")
    block = block.replace("zapstore.yaml", str(ROOT / "zapstore.yaml"))
    script = ("set -eo pipefail\nsleep(){ :; }\nfail(){ echo \"FAILED: $1\"; exit 1; }\n"
              + block
              + "\necho PUBLISHED\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return r, int(state.read_text())


def test_a_transient_outage_is_retried_until_it_publishes(tmp_path):
    r, attempts = _run(tmp_path, ["fail", "fail", "ok"])
    assert r.returncode == 0 and "PUBLISHED" in r.stdout, r.stdout + r.stderr
    assert attempts == 3


def test_an_appcatalog_rejection_is_retried_too(tmp_path):
    r, attempts = _run(tmp_path, ["reject", "ok"])
    assert r.returncode == 0 and attempts == 2, r.stdout


def test_a_lasting_outage_still_fails_the_job_and_says_why(tmp_path):
    r, attempts = _run(tmp_path, ["fail"] * 6)
    assert r.returncode != 0 and "zsp could not publish a complete release" in r.stdout, r.stdout
    assert attempts == 4, "gave up after %d attempts" % attempts
    r, _ = _run(tmp_path, ["reject"] * 6)
    assert "Zapstore AppCatalog rejected the clean APK asset" in r.stdout, r.stdout


def test_accepted_but_not_stored_is_not_published(tmp_path):
    """1.0.2505: zsp exited 0 and the relay ACCEPTED the asset, but held it (the APK upload had timed out). The
    read-back says the asset is not there, so the job retries -- and fails loudly instead of printing Published."""
    (tmp_path / "not_held").write_text("x")
    r, attempts = _run(tmp_path, ["ok"] * 6)
    assert r.returncode != 0 and "does not hold its APK asset" in r.stdout, r.stdout
    assert attempts == 4


def test_a_release_counts_only_once_zapstore_promoted_it():
    """2026-10-27, Zapstore: releases are verified, then promoted, "a few minutes max" -- and an asset can be held while
    its release is refused (the 2026-10-26 lineage refusal). The job waits for the PROMOTED kind-30063 after the publish
    loop, never republishes for it (a second asset would be an orphan 3063, which the new check refuses), and the
    asset wait is long enough for verification."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import zapstore_verify as Z
    rel = {"kind": 30063, "tags": [["d", "place.poster.app@1.0.2621"], ["version", "1.0.2621"]]}
    assert Z.has_release([rel], "place.poster.app", "1.0.2621")
    assert not Z.has_release([rel], "place.poster.app", "1.0.2622")
    assert not Z.has_release([{**rel, "kind": 3063}], "place.poster.app", "1.0.2621")
    assert Z.release_filter("ab", "place.poster.app", "1.0.2621") == {
        "kinds": [30063], "authors": ["ab"], "#d": ["place.poster.app@1.0.2621"]}
    wf = (ROOT / ".github/workflows/android.yml").read_text()
    loop = _block()
    assert "--wait 600" in loop and "--release" not in loop, "the publish loop must wait for the asset, not the release"
    after = wf[wf.index(loop.splitlines()[-1], wf.index("published=''")):]
    assert "--release place.poster.app" in after[:after.index("Published 1.0.")], "no promotion check before 'Published'"
