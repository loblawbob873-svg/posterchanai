"""A test that can be RUN AS A SCRIPT must not write the node's live local_settings.json.

tests/conftest.py redirects `settings_store._LOCAL_PATH` to a tmp dir for every pytest test, but a file
run as `python tests/x.py` never loads conftest. `tests/test_git_proxy.py` is such a file, and its
`finally:` did `settings_store.put("git_server_proxy_url", "")` -- a LOCAL-ONLY key, so the put went
straight into /var/lib/posterchanai/local_settings.json. On 2026-10-10 that blanked server1's
`git_server_proxy_url` (it was http://nas.lan:3053): poster.place/git answered 404 "not a git proxy node",
the nostr git remote stopped serving, and nas.lan could not pull deploy 173. Nothing logged it; the value
just changed.

The rule: any tests/*.py with a `__main__` entry that writes settings must point the local file somewhere
else (`_LOCAL_PATH` or POSTERCHANAI_LOCAL_SETTINGS) before it does.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTS = os.path.join(ROOT, "tests")

_WRITES = re.compile(r"\b(?:settings_store|ss)\.put\(")
_ISOLATES = re.compile(r"_LOCAL_PATH|POSTERCHANAI_LOCAL_SETTINGS")


def _script_tests_that_write_settings():
    for name in sorted(os.listdir(TESTS)):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(TESTS, name), encoding="utf-8").read()
        if "__main__" in src and _WRITES.search(src):
            yield name, src


def test_every_script_test_that_writes_settings_isolates_the_local_file():
    leaks = [n for n, src in _script_tests_that_write_settings() if not _ISOLATES.search(src)]
    assert not leaks, ("these tests write settings when run as scripts, where conftest's isolation does not "
                       "apply, and would rewrite the node's live local_settings.json: %s" % leaks)


def test_the_git_proxy_test_module_does_not_point_at_the_live_file():
    """Behaviour, not just text: importing the module (what `python tests/test_git_proxy.py` does first)
    must leave settings_store writing somewhere other than the default live path."""
    code = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
            "import test_git_proxy; from app.services import settings_store as s;"
            "print(s._LOCAL_PATH)") % (ROOT, TESTS)
    env = {k: v for k, v in os.environ.items() if k not in ("POSTERCHANAI_LOCAL_SETTINGS", "POSTERCHANAI_DATA")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    path = out.stdout.strip().splitlines()[-1]
    assert path != "/var/lib/posterchanai/local_settings.json", path
    assert not path.startswith("/var/lib/posterchanai/"), path
