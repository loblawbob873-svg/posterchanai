"""A CLIENT CHANGE THAT DOES NOT BUMP THE SERVICE WORKER IS A CHANGE NOBODY RECEIVES.

The service worker serves app code STALE-WHILE-REVALIDATE, so `const CACHE = 'pc-nostr-vNNNN'` is
the only thing that surfaces a new build: bumping it forces a reinstall and fires `controllerchange`,
which drives the "🔄 Update available" prompt. Ship a client change without it and every PWA and APK
user keeps the cached app — the fix appears not to deploy, there is no error anywhere, and the next
report is "it still does X".

It is a hand-bump by design (the repo's own note says so, and the number is meaningful to people
reading release history), which is exactly why it needs a guard rather than a convention. It was
forgotten twice in one day here — once for seven changed client files, once for the batch after it.

THIS IS A DEPLOY-SHAPED RULE, so it reads git: if what this deploy carries (origin/master..HEAD) touches the client
bundle, the cache version at HEAD must differ from production's. Nothing about the working tree, so it is meaningful in CI and on a
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


def _is_bundle(path: str) -> bool:
    return path != SW and any(path.startswith(p) for p in BUNDLE_PREFIXES)


def test_git_is_readable_here():
    """Without this the rule below passes on any tree where git is absent, which is worse than no
    rule — it would look like coverage."""
    assert _git("rev-parse", "HEAD").strip(), "not a git checkout; this guard cannot run"


def _deploy_base() -> str:
    """What production runs: the merge-base with origin/master. A DEPLOY is the unit that reaches users, not a
    commit -- checking only HEAD refused a deploy whose earlier commit had bumped the cache (2026-10-28, a lost 25-min
    gate). No origin (CI on the mirror, a fresh clone) or nothing ahead of it: the last commit, as before."""
    base = _git("merge-base", "HEAD", "origin/master").strip()
    head = _git("rev-parse", "HEAD").strip()
    if not base or base == head:
        base = _git("rev-parse", "HEAD^").strip()
    return base


def _range_files(base: str) -> list[str]:
    out = _git("diff", "--name-only", base, "HEAD")
    return [l.strip() for l in out.splitlines() if l.strip()]


def _cache_at(rev: str) -> str:
    text = _git("show", "%s:%s" % (rev, SW))
    return next((l.strip() for l in text.splitlines() if "const CACHE" in l), "")


def test_the_deploy_bumped_the_cache_if_it_touched_the_client():
    base = _deploy_base()
    if not base:
        pytest.skip("no parent commit to compare with")
    bundle = sorted(f for f in _range_files(base) if _is_bundle(f))
    if not bundle:
        return                      # nothing cached changed; no bump is owed
    assert _cache_at("HEAD") and _cache_at("HEAD") != _cache_at(base), (
        "this deploy changes the cached client bundle (%s) but the service worker cache version in %s is the one "
        "production already has (%s). Every PWA and APK user keeps the stale app and the change appears not to "
        "deploy." % (", ".join(bundle[:6]), SW, _cache_at(base)))


def test_the_constant_is_shaped_the_way_the_updater_expects():
    sw = (ROOT / SW).read_text(encoding="utf-8")
    line = [l for l in sw.splitlines() if "const CACHE" in l]
    assert line, "the cache constant is gone — the update prompt has nothing to fire on"
    assert "pc-nostr-v" in line[0], line[0]
