import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_secure_tar_override_keeps_capacitor_sync_compatible():
    pkg = json.loads((ROOT / "mobile/package.json").read_text())
    assert pkg["overrides"]["tar"].startswith("7.")
    assert pkg["scripts"]["postinstall"] == "node scripts/patch-capacitor-tar.cjs"
    patch = (ROOT / "mobile/scripts/patch-capacitor-tar.cjs").read_text()
    assert "tar_1.default.extract" in patch
    assert "tar_1.extract" in patch
    assert "refusing an unverified" in patch
