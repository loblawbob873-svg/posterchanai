"""Zapstore checks only an asset's FIRST apk_certificate_hash against its pin (probed 2026-10-25). It pinned
place.poster.app to the retired signer (eddf…) from 2026-10-24 and re-pinned it to the current one (c696…) on
2026-10-25 (zapstore/relay#10). scripts/zapstore_first_cert.py guards the real `zsp publish --offline` output before
every publish against the pin the workflow names; this runs it on the shapes that were refused and accepted."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETIRED, ACTIVE = "eddf3a7983df49221a5ace0d0ca52c899d34eb88a4155b0829b05c0afc31f342", \
    "c696595f4ffa6e5f9f010fb0d9faedda2faffa42ba38338163381227f935b31b"
PIN = ACTIVE   # what relay.zapstore.dev pins today


def _run(tmp_path, hashes):
    lines = [json.dumps({"kind": 32267, "tags": [["d", "place.poster.app"]]}),
             json.dumps({"kind": 3063, "tags": [["i", "place.poster.app"]] + [["apk_certificate_hash", h] for h in hashes]})]
    p = tmp_path / "events.json"
    p.write_text("\n".join(lines) + "\n")
    return subprocess.run([sys.executable, str(ROOT / "scripts/zapstore_first_cert.py"), str(p), PIN],
                          capture_output=True, text=True)


def test_leading_with_anything_but_the_pin_fails_the_check(tmp_path):
    assert _run(tmp_path, [RETIRED]).returncode == 1, "the retired signer alone -- refused since the re-pin"
    assert _run(tmp_path, [RETIRED, ACTIVE]).returncode == 1, "deploy 62's shape, retired FIRST -- refused since the re-pin"
    assert _run(tmp_path, []).returncode == 1, "no certificate at all"


def test_the_pin_first_passes(tmp_path):
    for hashes in ([ACTIVE], [ACTIVE, RETIRED]):
        r = _run(tmp_path, hashes)
        assert r.returncode == 0, (hashes, r.stdout + r.stderr)


def test_no_asset_at_all_is_a_failure_not_a_pass(tmp_path):
    p = tmp_path / "events.json"
    p.write_text("")
    r = subprocess.run([sys.executable, str(ROOT / "scripts/zapstore_first_cert.py"), str(p), PIN], capture_output=True, text=True)
    assert r.returncode == 1


def test_the_workflow_sets_the_pin_rebuilds_zsp_and_checks_before_publishing():
    wf = (ROOT / ".github/workflows/android.yml").read_text()
    patch = (ROOT / "scripts/zsp-signing-lineage.patch").read_text()
    assert "ZSP_ANDROID_PINNED_CERTIFICATE_HASH: " + PIN in wf
    assert 'os.Getenv("ZSP_ANDROID_PINNED_CERTIFICATE_HASH")' in patch
    # The patch change only reaches CI through a rebuilt binary: the old cache key restores the old zsp.
    assert "zsp-v0.4.11-posterchan-lineage-v3" not in wf
    check, publish = wf.index("scripts/zapstore_first_cert.py"), wf.index("/tmp/zsp publish -q zapstore.yaml")
    assert check < publish, "the pin check must run before the publish"
