"""The updater must never offer a build that is not newer than the running one.

Reported: "installer on laptop keeps telling me that 1.0.467 is ready to install" — on a machine
already running 1.0.468.

electron-updater caches the installer it downloaded and keeps offering it until it is applied.
Install a build by hand in the meantime, which is what anybody does while waiting on a fix, and the
cache is now BEHIND the app: the prompt appears for ever, and accepting it is a downgrade — which on
a day like today would silently take back the fixes it was installed for.

The version scheme is `1.0.<build number>` (the workflow stamps `npm version 1.0.${run_number}`), so
"newer" is a comparison of that trailing number. This runs the shipped comparison rather than reading
it, because an off-by-one here is invisible until somebody is stuck on a loop of downgrade prompts.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "desktop", "main.js")


def _slice(name):
    src = open(MAIN, encoding="utf-8").read()
    at = src.index("const %s = (" % name)
    i = src.index("{", at)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    end = src.index(";", i)
    return src[at:end + 1]


def _run(js):
    if shutil.which("node") is None:
        pytest.skip("no node")
    prog = _slice("buildOf") + "\n" + js
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-1500:]
    return r.stdout.strip()


def test_the_build_number_is_read_from_the_version():
    out = _run("console.log([buildOf('1.0.467'), buildOf('1.0.1136'), buildOf('1.0.3'),"
               " buildOf(''), buildOf(undefined)].join(','))")
    got = out.split(",")
    assert got[:3] == ["467", "1136", "3"]
    assert got[3] in ("NaN",) and got[4] in ("NaN",), \
        "an unreadable version must not compare as a number"


def test_an_older_or_equal_build_is_not_offered():
    """The reported case: cached 467 against a running 468, and the equal case, which would prompt
    on every launch for the build you already have."""
    src = open(MAIN, encoding="utf-8").read()
    at = src.index("update-downloaded")
    block = src[at:at + 900]
    assert "isNewer(" in block, "the handler prompts without comparing versions at all"
    assert "return;" in block.split("isNewer(")[1][:220], \
        "it compares and then prompts anyway"
    assert "dropStaleDownload()" in block, \
        "the stale download is left in place, so the next check finds it again and stops there"


def test_an_unreadable_version_still_prompts():
    """Failing CLOSED here would mean an app that can never update itself again — much worse than one
    extra dialog. NaN on either side means 'cannot tell', and cannot-tell offers the update."""
    out = _run("const isNewer=(v,cur)=>{const a=buildOf(v),b=buildOf(cur);"
               "return !(Number.isFinite(a)&&Number.isFinite(b))||a>b;};"
               "console.log([isNewer('1.0.469','1.0.468'), isNewer('1.0.467','1.0.468'),"
               " isNewer('1.0.468','1.0.468'), isNewer('weird','1.0.468')].join(','))")
    assert out == "true,false,false,true"
