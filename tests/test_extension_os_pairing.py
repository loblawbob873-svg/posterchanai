"""PosterChan Passwords pairs itself on PosterChanOS, with nothing to type.

"we need to make sure that the passwords extension automatically connects to posterchanOS without any config"
(2026-10-29). The signed-in desktop writes this account's pairing into Firefox's per-user managed storage for the add-on;
the shipped background.js reads it at start and every minute. Run under node with a fake browser.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["paired-at-start", "later-sign-in", "signed-out", "account-switch", "unchanged",
                                      "user-pairing-wins", "no-managed-storage"])
def test_os_pairing(scenario, tmp_path):
    for name in ("background.js", "vaultcore.js"):
        shutil.copyfile(ROOT / "extension" / name, tmp_path / name)
    if not (ROOT / "extension" / "vaultcore.js").exists():
        shutil.copyfile(ROOT / "static/js/client/vaultcore.js", tmp_path / "vaultcore.js")
    (tmp_path / "vendor").mkdir()
    shutil.copyfile(ROOT / "static/vendor/nostr/nostr.bundle.js", tmp_path / "vendor/nostr.bundle.js")
    r = subprocess.run(["node", "--unhandled-rejections=strict", str(ROOT / "tests/extension_os_pairing.mjs"), str(tmp_path),
                        scenario], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
