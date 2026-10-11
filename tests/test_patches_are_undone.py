"""A patch a test starts must be undone, and undone in the REVERSE of the order it was made (deploy 205).

The gate's full suite runs its files in a few parallel shards, one pytest process each. ~90 tests in one shard
(test_jellyfin, test_instance_welcome, test_multiple_nip05_names, test_wave3_users, test_texts_ai_reply's native
auth, test_media_center, test_no_postgres_fresh_node ...) failed with "'_User' object has no attribute 'is_admin'"
/ "'username'" / "'changes'" and passed alone, so the serial re-run filed them as FLAKY. They were not flaky:
`user_table.aby_npub` -- the account lookup behind every signed request -- was still the stub
`AsyncMock(return_value=_User())` from tests/test_doc_endpoints_refuse_an_unreachable_relay.py, so every later
sign-in in that process was a two-attribute fake.

How: that file's `_Base.setUp` patches `aby_npub` and stops its patches in START order; tests/
test_drafts_nostr_only_accounts.py's subclass patches `aby_npub` AGAIN (an account-less member) and appends that
patcher to the same list. A patcher restores what was there when IT started, so stopping first-to-last put the
real function back and then the second stop put the first patch's stub back on top of it -- for good.

Two guards: the rule itself, read from every test file (a loop that `.stop()`s patchers must walk them
`reversed(...)`), and the observable: the leaking files run in ONE process, followed by tests that sign in, with a
plugin reporting any `app.*` function or class left replaced at the end.
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The files that share the patcher list (the leaker and its base), then tests that READ the account lookup.
LEAKERS = [
    "tests/test_doc_endpoints_refuse_an_unreachable_relay.py",
    "tests/test_drafts_nostr_only_accounts.py",
    "tests/test_sync_state_endpoint.py",
]
VICTIMS = [
    "tests/test_instance_welcome.py::test_real_admin_grant_requires_admin_signature_and_sends_approval",
    "tests/test_wave3_users.py::test_a_nostr_sign_in_is_still_a_session_in_a_fresh_process",
    "tests/test_texts_ai_reply.py::NativeAuthTests",
]


def _forward_stop_loops(source: str):
    """(line, loop source) of every `for p in X: ... p.stop()` (or comprehension) whose X is not reversed(...)."""
    with warnings.catch_warnings():         # other files' invalid escapes are not this test's business
        warnings.simplefilter("ignore")
        tree = ast.parse(source)
    bad = []

    def stops(body_nodes, var):
        for node in body_nodes:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "stop"
                        and isinstance(sub.func.value, ast.Name) and sub.func.value.id == var and not sub.args):
                    return True
        return False

    def is_reversed(it):
        return isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "reversed"

    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if stops(node.body, node.target.id) and not is_reversed(node.iter):
                bad.append((node.lineno, ast.get_source_segment(source, node).splitlines()[0]))
        elif isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
            for gen in node.generators:
                if isinstance(gen.target, ast.Name) and stops([node.elt], gen.target.id) and not is_reversed(gen.iter):
                    bad.append((node.lineno, ast.get_source_segment(source, node)))
    return bad


def test_the_rule_catches_the_shape_that_leaked():
    """The check itself must be able to fail: the pre-fix tearDown, verbatim."""
    leaked = "def tearDown(self):\n    for p in self.patches:\n        p.stop()\n"
    fixed = "def tearDown(self):\n    for p in reversed(self.patches):\n        p.stop()\n"
    assert _forward_stop_loops(leaked) == [(2, "for p in self.patches:")]
    assert _forward_stop_loops(fixed) == []
    assert _forward_stop_loops("[p.stop() for p in ps]\n")


def test_no_test_stops_its_patchers_in_start_order():
    offenders = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        try:
            source = path.read_text()
            found = _forward_stop_loops(source)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        offenders += ["%s:%d  %s" % (path.relative_to(ROOT), line, text) for line, text in found]
    assert not offenders, (
        "patchers stopped in START order -- when two patch the same attribute, the last stop puts the first "
        "one's stub back for the rest of the process. Walk them reversed(...) (or use addCleanup):\n  "
        + "\n  ".join(offenders))


def test_the_leaking_files_leave_nothing_patched_for_the_next_test():
    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "leaks.json")
        env = dict(os.environ, PC_PATCH_LEAK_REPORT=report)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--noconftest", "-o", "addopts=", "-q", "--disable-warnings", "-p", "no:cacheprovider",
             "-p", "tests.patch_leak_probe", *LEAKERS, *VICTIMS],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        leaked = json.loads(Path(report).read_text()) if os.path.exists(report) else ["<no report written>"]
    tail = "\n".join(proc.stdout.splitlines()[-30:])
    assert leaked == [], "a test left these replaced for every later test in its process:\n  %s\n%s" % (
        "\n  ".join(leaked), tail)
    assert proc.returncode == 0, (
        "tests that sign in failed after the patching files ran in the same process (they pass alone):\n" + tail)
