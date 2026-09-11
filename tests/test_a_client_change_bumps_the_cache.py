"""A CLIENT CHANGE THAT DOES NOT BUMP THE SERVICE WORKER IS A CHANGE NOBODY RECEIVES.

The service worker serves app code STALE-WHILE-REVALIDATE, so `const CACHE = 'pc-nostr-vNNNN'` is
the only thing that surfaces a new build: bumping it forces a reinstall and fires `controllerchange`,
which drives the "🔄 Update available" prompt. Ship a client change without it and every PWA and APK
user keeps the cached app — the fix appears not to deploy, there is no error anywhere, and the next
report is "it still does X".

It is a hand-bump by design (the repo's own note says so, and the number is meaningful to people
reading release history), which is exactly why it needs a guard rather than a convention. It was
forgotten twice in one day here — once for seven changed client files, once for the batch after it.

THIS IS A COMMIT-SHAPED RULE, so it reads git: if the commit under test touched the client bundle,
it must have touched `sw.js` too. Nothing about the working tree, so it is meaningful in CI and on a
fresh clone.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Files the SW serves to a cached client. A change to any of them reaches nobody without a bump.
BUNDLE_PREFIXES = ("static/js/client/", "static/css/", "templates/client.html")
SW = "static/js/client/sw.js"


def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout


def _changed(rev: str) -> list[str]:
    out = _git("show", "--name-only", "--pretty=format:", rev)
    return [l.strip() for l in out.splitlines() if l.strip()]


def _is_bundle(path: str) -> bool:
    return path != SW and any(path.startswith(p) for p in BUNDLE_PREFIXES)


def test_git_is_readable_here():
    """Without this the rule below passes on any tree where git is absent, which is worse than no
    rule — it would look like coverage."""
    assert _git("rev-parse", "HEAD").strip(), "not a git checkout; this guard cannot run"


def test_the_last_commit_bumped_the_cache_if_it_touched_the_client():
    files = _changed("HEAD")
    if not files:
        pytest.skip("HEAD is a merge or empty commit")
    bundle = sorted(f for f in files if _is_bundle(f))
    if not bundle:
        return                      # nothing cached changed; no bump is owed
    assert SW in files, (
        "HEAD changes the cached client bundle (%s) without bumping the service worker cache "
        "version in %s. Every PWA and APK user keeps the stale app and the change appears not to "
        "deploy." % (", ".join(bundle[:6]), SW))


def test_the_bump_actually_changed_the_constant():
    """Touching the file is not the same as bumping it — a comment edit would satisfy a weaker
    rule and change nothing for anybody."""
    files = _changed("HEAD")
    if SW not in files or not any(_is_bundle(f) for f in files):
        return
    diff = _git("show", "HEAD", "--", SW)
    added = [l for l in diff.splitlines() if l.startswith("+") and "const CACHE" in l]
    removed = [l for l in diff.splitlines() if l.startswith("-") and "const CACHE" in l]
    assert added and removed, (
        "%s was touched but `const CACHE` did not change, so no client will reinstall" % SW)
    assert added[0] != removed[0].replace("-", "+", 1)


def test_the_constant_is_shaped_the_way_the_updater_expects():
    sw = (ROOT / SW).read_text(encoding="utf-8")
    line = [l for l in sw.splitlines() if "const CACHE" in l]
    assert line, "the cache constant is gone — the update prompt has nothing to fire on"
    assert "pc-nostr-v" in line[0], line[0]
