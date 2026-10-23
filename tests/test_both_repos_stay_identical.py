"""Both repositories carry the same branch tips after every deploy, and the mirror's master never doubles a build.

2026-10-22, "make sure both repos are on the same level and consistent": the nostr host's `main` sat on an
October 13 commit and the GitHub mirror's `master` two months behind production, because sync.sh pushed neither.
Pushing the mirror's master used to re-run every app build (the workflows triggered on main AND master) and Mozilla
refused the duplicate add-on upload with "Conflict". So: sync.sh pushes and VERIFIES all four tips, and the
workflows build from the mirror's `main` only.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC = (ROOT / "sync.sh").read_text(encoding="utf-8")


def test_sync_pushes_all_four_branch_tips():
    for push in ("_push_github master:main", "_push_github master:master", "git push origin master:main"):
        assert push in SYNC, push
    # a DNS blip must not skip the app builds: the GitHub pushes are retried
    assert re.search(r'_push_github\(\) \{ for _try in 1 2 3; do git push github "\$1" && return 0; sleep 10; done; return 1; \}', SYNC)
    # `git push` itself moves origin/master (the deploy); the explicit refspecs move the other three.
    assert re.search(r"for _r in origin:master origin:main github:main github:master; do", SYNC), \
        "the end-of-deploy check does not verify every branch tip on both repositories"


def test_no_workflow_builds_from_the_mirrors_master():
    offenders = []
    for wf in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for line in wf.read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*branches:\s*\[", line) and "master" in line.split("#", 1)[0]:
                offenders.append(f"{wf.name}: {line.strip()}")
    assert not offenders, ("a push to the mirror's master would run a second set of app builds", offenders)
