"""Upstream changes fail the check without silently accepting a new baseline."""
import json
from pathlib import Path
from scripts import check_jellyfin_upstream as check


def test_source_and_sdk_drift_require_explicit_review(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    models = repo / 'tests/jellyfin/kotlin_contract.json'
    models.parent.mkdir(parents=True)
    models.write_text(json.dumps({'models': {}}))
    monkeypatch.setattr(check, 'ROOT', repo)
    baseline = models.with_name('upstream.json')
    monkeypatch.setattr(check, 'BASELINE', baseline)
    sources = tmp_path / 'sources'
    for name in check.ROKU_FILES:
        path = sources / 'roku' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('reviewed source')
    package = sources / 'npm/node_modules/@jellyfin/sdk/package.json'
    package.parent.mkdir(parents=True)
    package.write_text('{"version":"1.0.0"}')
    assert check.check(sources, record=True) == 0
    reviewed = baseline.read_bytes()
    assert check.check(sources, drift_only=True) == 0
    (sources / 'roku' / check.ROKU_FILES[0]).write_text('changed client behavior')
    assert check.check(sources, drift_only=True) == 1
    assert baseline.read_bytes() == reviewed
    (sources / 'roku' / check.ROKU_FILES[0]).write_text('reviewed source')
    package.write_text('{"version":"1.1.0"}')
    assert check.check(sources, drift_only=True) == 1
    assert baseline.read_bytes() == reviewed
