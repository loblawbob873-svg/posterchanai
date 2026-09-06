"""Generated artifacts the suite needs, made once per session so a FRESH CLONE can run.

THE SUITE COULD NOT PASS ON A CHECKOUT OF THIS REPO, and the only reason nobody noticed is that the
machine it is usually run on had built them months ago. Nine tests failed on every clone — in the
sandbox, in a container, in CI, for anybody who cloned it — with messages that pointed at the code
rather than at the environment:

    ENOENT: no such file or directory, open 'extension/vendor/nostr.bundle.js'
    shell.html references icon.png, missing

Both are COPIES of files that ARE tracked (`static/vendor/nostr/nostr.bundle.js`,
`static/icon-512.png`), made by `extension/build.sh` and desktop's `npm run icon`, and both land in
gitignored directories. So the artifact is missing, not the source — nothing needs downloading and
nothing needs building, it is two file copies.

This is the same trap CLAUDE.md records for ACE-Step: one node's working tree had been hand-edited,
so that box looked fine while every fresh clone and Docker build was broken. It cost a full morning
here, because 9 environmental failures are indistinguishable from 9 real ones in a `--brief` report
and they buried the two failures that were real.

Copies, deliberately, rather than invoking the build scripts: `extension/build.sh` also generates
icons and writes `dist/`, and `npm run icon` needs node — this needs neither, runs in milliseconds,
and is idempotent, so it is a no-op on a machine that has already built. It writes only into
gitignored build directories, never into live state (the rule a check that touched
`streamserver/mediamtx.pid` broke).
"""
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """A settings update in one test must not reconfigure the next test or this host."""
    from app.services import settings_store
    monkeypatch.setattr(settings_store, '_CACHE', {})
    monkeypatch.setattr(settings_store, '_LOCAL_DIRTY', set())
    monkeypatch.setattr(settings_store, '_HYDRATED_KEYS', set())
    monkeypatch.setattr(settings_store, '_loaded', False)
    monkeypatch.setattr(settings_store, '_LOCAL_PATH', str(tmp_path / 'local_settings.json'))

# (generated artifact, tracked source, what breaks without it)
_ARTIFACTS = [
    (ROOT / "extension" / "vendor" / "nostr.bundle.js",
     ROOT / "static" / "vendor" / "nostr" / "nostr.bundle.js",
     "extension/build.sh copies it; without it the extension's service worker dies at load and "
     "every test_extension_worker_boot / test_vault_signer case fails"),
    (ROOT / "desktop" / "icon.png",
     ROOT / "static" / "icon-512.png",
     "desktop's `npm run icon` copies it; shell.html references it and test_desktop_packaging "
     "asserts every referenced file is present"),
]


@pytest.fixture(scope="session", autouse=True)
def _generated_artifacts():
    """Make the copies if they are absent. Never overwrites: a machine that has built (or is
    deliberately testing a different bundle) keeps exactly what it has."""
    for dest, src, why in _ARTIFACTS:
        if dest.exists() or not src.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"[conftest] generated {dest.relative_to(ROOT)} from "
              f"{src.relative_to(ROOT)} — {why}")
