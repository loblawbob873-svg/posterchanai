"""A server-only deploy scans the test tree for browser tests ONCE, not once per stage that needs the answer.

browser_test_files(root) reads every file under tests/ and follows imports transitively (~0.3 s on this tree,
642 browser files). A server-only deploy needed its answer four times in one gate process: the preflight's
selection, the preflight's own run_full_suite, the required list's filter in main(), and the full suite's
run_full_suite -- each a full re-scan of a tree that does not change while the gate runs (the gate refuses a
tree that changed under it: source_fingerprint). Driven through the real run_preflight and run_full_suite (only
the shard runner and the git diff are stood in for); scans are counted at the launch-pattern search, one per file
per scan.
"""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tree(tmp_path):
    t = tmp_path / "tests" / "client"
    t.mkdir(parents=True)
    (tmp_path / "tests" / "__init__.py").write_text("")
    (t / "__init__.py").write_text("")
    (t / "test_launches.py").write_text("CMD=['chrome','--headless=new']\ndef test_x(): pass\n")
    (t / "test_through.py").write_text("from tests.client.test_launches import CMD\ndef test_x(): pass\n")
    for i in range(6):
        (tmp_path / "tests" / ("test_server_%d.py" % i)).write_text("def test_x(): pass\n")
    return sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "tests").rglob("test_*.py"))


def test_one_gate_run_scans_for_browser_tests_once(tmp_path, monkeypatch):
    files = _tree(tmp_path)
    g = runpy.run_path(str(ROOT / "scripts/deploy_regression_gate.py"))
    G = g["run_preflight"].__globals__
    real = G["_LAUNCH"]
    searches = []

    class Counting:
        def search(self, s):
            searches.append(1)
            return real.search(s)
    monkeypatch.setitem(G, "_LAUNCH", Counting())
    monkeypatch.setitem(G, "discover_test_files", lambda root: files)
    monkeypatch.setitem(G, "deploy_changes", lambda root: ["app/services/x.py"])
    monkeypatch.setitem(G, "affected_test_files", lambda root, p, f: ["tests/test_server_0.py", "tests/client/test_through.py"])
    ran = []
    monkeypatch.setitem(G, "_run_shards", lambda root, env, d, f, *a, **k: (ran.append(list(f)), (True, "ok"))[1])
    monkeypatch.setenv("PC_GATE_MANAGED_PROCESSES", "1")
    monkeypatch.setitem(G, "PREFLIGHT_MAX_SHARE", 1.0)

    # The stages of a server-only deploy, in main()'s order.
    did, ok, _msg, passed = G["run_preflight"](tmp_path, {}, str(tmp_path), jobs=1, skip_browser=True)
    assert (did, ok) and passed == {"tests/test_server_0.py"}
    browser = G["browser_test_files"](tmp_path)                       # main(): the required list's filter
    assert browser == {"tests/client/test_launches.py", "tests/client/test_through.py"}
    ok, _ = G["run_full_suite"](tmp_path, {}, str(tmp_path), jobs=1, skip_browser=True,
                                select=lambda f: f not in passed)
    assert ok and ran[-1] and not set(ran[-1]) & browser, ran        # the answer itself is unchanged
    scans = len(searches) / len(list((tmp_path / "tests").rglob("*.py")))
    print("browser-test scans in one server-only gate run: %g" % scans)
    assert scans == 1, ("the test tree was scanned for browser tests %g times in one gate run" % scans)


def test_the_answer_is_a_copy_a_caller_may_change(tmp_path):
    _tree(tmp_path)
    g = runpy.run_path(str(ROOT / "scripts/deploy_regression_gate.py"))
    first = g["browser_test_files"](tmp_path)
    first.clear()
    assert g["browser_test_files"](tmp_path) == {"tests/client/test_launches.py", "tests/client/test_through.py"}
