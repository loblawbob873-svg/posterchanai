"""A server-only deploy skips the browser tests; anything a browser could notice runs them all.

"yes skip browser checks for server-only deploys": the browser tests are about half the gate, and a deploy that
changes nothing a browser loads cannot be caught by one. What this file holds the gate to:

  * a browser test is recognised from its own source -- it starts Chrome, runs a scripts/check_* browser check,
    or imports a test module that does (transitively) -- so a new one is classified by being written, while a
    node- or source-reading test under tests/client/ keeps running;
  * "server-only" is read from the deploy's real diff (origin/master...HEAD plus what sync.sh will commit), and
    every doubt runs them: a router, app/main.py, static/, templates/, desktop/, mobile/, os/, a browser test,
    scripts/check_*, the gate itself, an unreadable diff, an empty diff, or PC_GATE_FULL_BROWSER=1;
  * on a server-only deploy the full suite really leaves the browser files out -- and nothing else.
"""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _gate():
    spec = importlib.util.spec_from_file_location("pc_gate_under_test", ROOT / "scripts/deploy_regression_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("path,server_only", [
    ("app/services/posterchandb/store.py", True),
    ("app/services/nostr_relay/store.py", True),
    ("app/models.py", True),
    ("app/schemas.py", True),
    ("botframework/main.py", True),
    ("docs/POSTERCHANDB.md", True),
    ("CLAUDE.md", True),
    ("requirements.txt", True),
    ("tests/test_posterchandb.py", True),
    ("scripts/posterchandb_parity.py", True),
    ("app/routers/admin.py", False),           # routers shape what the client receives
    ("app/main.py", False),
    ("static/js/client/app.js", False),
    ("static/css/client.css", False),
    ("templates/admin/tabs/bots.html", False),
    ("desktop/main.js", False),
    ("mobile/android/app/build.gradle", False),
    ("os/gentoo.sh", False),
    ("tests/client/test_posterchandb_mirror_full_app.py", False),
    ("scripts/check_client_mobile.py", False),
    ("scripts/deploy_regression_gate.py", False),
    ("sync.sh", False),
])
def test_what_counts_as_server_only(path, server_only):
    assert _gate().is_server_only_path(path) is server_only


def test_browser_tests_are_recognised_from_their_source(tmp_path):
    t = tmp_path / "tests" / "client"
    t.mkdir(parents=True)
    (tmp_path / "tests" / "__init__.py").write_text("")
    (t / "__init__.py").write_text("")
    (t / "test_launches.py").write_text("CMD=['chrome','--headless=new']\ndef test_x(): pass\n")
    (t / "helper_browser.py").write_text("ARGS=['--remote-debugging-port=0']\n")
    (t / "test_through_a_helper.py").write_text("from tests.client.helper_browser import ARGS\ndef test_x(): pass\n")
    (t / "test_through_a_test.py").write_text("from tests.client.test_through_a_helper import ARGS\ndef test_x(): pass\n")
    (t / "test_runs_a_check.py").write_text("subprocess.run(['python','scripts/check_notes_mobile.py'])\n")
    (t / "test_node_only.py").write_text("subprocess.run(['node','sim.js'])\ndef test_x(): pass\n")
    (tmp_path / "tests" / "test_mentions_chromium.py").write_text("PKG='www-client/chromium'\ndef test_x(): pass\n")
    got = _gate().browser_test_files(tmp_path)
    assert {"tests/client/test_launches.py", "tests/client/test_through_a_helper.py",
            "tests/client/test_through_a_test.py", "tests/client/test_runs_a_check.py"} <= got
    assert "tests/client/test_node_only.py" not in got
    assert "tests/test_mentions_chromium.py" not in got


def test_the_real_suites_browser_tests_are_found():
    got = _gate().browser_test_files(ROOT)
    for known in ("tests/client/test_bot_list_fields_full_app.py", "tests/client/test_concord_every_button_full_app.py",
                  "tests/client/test_effects_full_app.py"):
        assert known in got, known
    for pure in ("tests/test_posterchandb.py", "tests/test_bot_list_fields.py",
                 "tests/client/test_a_default_instance_is_not_an_answer.py"):
        assert pure not in got, pure


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True)  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    for f in ("app/services/x.py", "static/js/client/app.js", "app/routers/admin.py"):
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text("one\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/master", "HEAD")
    return repo, git


@pytest.mark.parametrize("change,committed,skip", [
    ("app/services/x.py", True, True),
    ("app/services/x.py", False, True),            # what sync.sh is about to commit counts too
    ("static/js/client/app.js", True, False),
    ("static/js/client/app.js", False, False),     # an uncommitted client edit is in this deploy as well
    ("app/routers/admin.py", True, False),
])
def test_the_decision_reads_the_deploys_own_diff(tmp_path, monkeypatch, change, committed, skip):
    monkeypatch.delenv("PC_GATE_FULL_BROWSER", raising=False)
    repo, git = _repo(tmp_path)
    (repo / change).write_text("two\n")
    if committed:
        git("commit", "-qam", "change")
    got, why = _gate().browser_skip_decision(repo)
    assert got is skip, why


def test_every_doubt_runs_the_browser_tests(tmp_path, monkeypatch):
    g = _gate()
    monkeypatch.delenv("PC_GATE_FULL_BROWSER", raising=False)
    repo, git = _repo(tmp_path)
    assert g.browser_skip_decision(repo)[0] is False, "an empty diff skipped"
    (repo / "app/services/x.py").write_text("two\n")
    monkeypatch.setenv("PC_GATE_FULL_BROWSER", "1")
    assert g.browser_skip_decision(repo)[0] is False, "the override did not force a full run"
    monkeypatch.delenv("PC_GATE_FULL_BROWSER")
    git("update-ref", "-d", "refs/remotes/origin/master")
    assert g.browser_skip_decision(repo)[0] is False, "an unreadable diff skipped"
    assert g.browser_skip_decision(tmp_path / "not-a-repo")[0] is False


def test_a_server_only_full_run_leaves_out_exactly_the_browser_files(monkeypatch, tmp_path):
    g = _gate()
    seen = {}

    def fake_shards(root, env, directory, files, shards, durations, captured):
        seen["files"] = list(files)
        return True, "%d files" % len(files)
    monkeypatch.setitem(g.__dict__, "_run_shards", fake_shards)
    monkeypatch.setenv("PC_GATE_MANAGED_PROCESSES", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    ok, message = g.run_full_suite(ROOT, dict(os.environ), str(tmp_path), jobs=2, skip_browser=True)
    everything = g.discover_test_files(ROOT)
    browser = g.browser_test_files(ROOT)
    assert ok and sorted(seen["files"]) == sorted(f for f in everything if f not in browser)
    assert "browser test file(s) skipped" in message
    ok, _ = g.run_full_suite(ROOT, dict(os.environ), str(tmp_path), jobs=2, skip_browser=False)
    assert sorted(seen["files"]) == sorted(everything), "a full run left something out"
