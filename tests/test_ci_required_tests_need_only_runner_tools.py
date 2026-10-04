"""A test the desktop CI requires must not depend on a tool the CI runner does not have.

2026-10-04: the deploy-79 desktop build failed -- so no overlay pin, no box update, no ISO -- because a new
browser test made its sample video with ffmpeg and skipped where ffmpeg is missing. The desktop CI runs
on a stock GitHub Ubuntu runner and counts a skipped required test as a failure (deploy_regression_gate:
"A skipped test is missing coverage"). Locally ffmpeg was there, so nothing said so until the build.

So: every browser test (tests/client/*_full_app.py, which that CI runs) and every test on the gate's
required list may only ask shutil.which() for tools the runner provides (or the workflow installs).
Anything else: commit a fixture (as tests/fixtures/concord_clip.webm now is), or install the tool in
.github/workflows/desktop.yml and add it here.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The tools required/browser tests already ask for, with CI passing (some are alternatives the test
# falls back between -- node/nodejs, Xvfb/wayfire/Xwayland, the chrome names). Recorded from the tree on
# 2026-10-04 rather than guessed: a NEW tool is the decision this test exists to force.
RUNNER_PROVIDES = {"node", "nodejs", "npm", "java", "javac", "python3", "git", "bash", "sh", "chrome", "chromium",
                   "google-chrome", "google-chrome-stable", "Xvfb", "Xwayland", "wayfire", "unshare"}


def _required():
    src = (ROOT / "scripts/deploy_regression_gate.py").read_text()
    block = src[src.index("TESTS = ("):src.index(")", src.index("TESTS = ("))]
    return {m.split("::")[0] for m in re.findall(r"'(tests/[^']+)'", block)}


def test_required_and_browser_tests_need_only_tools_the_ci_runner_has():
    files = set(_required()) | {str(p.relative_to(ROOT)) for p in (ROOT / "tests/client").glob("*_full_app.py")}
    bad = []
    for rel in sorted(files):
        p = ROOT / rel
        if not p.exists():
            continue
        for tool in re.findall(r"""which\(\s*['"]([A-Za-z0-9_.+-]+)['"]""", p.read_text(errors="replace")):
            if tool not in RUNNER_PROVIDES:
                bad.append((rel, tool))
    assert not bad, ("these tests skip on the CI runner (and a skip fails the desktop build) unless the tool "
                     "is installed there: %r -- commit a fixture instead, or install it in desktop.yml" % bad)
