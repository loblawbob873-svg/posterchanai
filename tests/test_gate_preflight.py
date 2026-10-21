"""The deploy gate runs the tests of what CHANGED first, and stops in minutes when they fail.

2026-10-21: three deploys in one afternoon each spent the whole ~20-minute gate to report one or two failures in a
test that named the very file that changed. The preflight selects those test files, runs them sharded with the
gate's own retry-alone rule, and aborts before the full run when they fail; when they pass nothing else changes.
"""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
G = runpy.run_path(str(ROOT / "scripts/deploy_regression_gate.py"))


def _tree(tmp_path, files):
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return sorted(n for n in files if Path(n).name.startswith("test_") and n.startswith("tests/"))


def test_selection_is_by_name_and_specific_stem_never_by_substring(tmp_path):
    files = _tree(tmp_path, {
        "tests/test_tg.py": "load('static/js/client/telegram.js')",
        "tests/test_mgr.py": "from app.services.telegram_client import manager",
        "tests/test_terminal.py": "the terminal determines nothing",        # 'term' must not match inside words
        "tests/test_term.py": "read('term.js')",
        "tests/test_app.py": "an app that does things",                   # generic stem: only the filename counts
        "tests/test_other.py": "unrelated",
    })
    pick = lambda paths: G["affected_test_files"](str(tmp_path), paths, files)
    assert pick(["static/js/client/telegram.js"]) == ["tests/test_tg.py"]
    assert pick(["static/js/client/term.js"]) == ["tests/test_term.py"], "a 4-letter stem must not select by substring"
    assert pick(["app/services/telegram_client/manager.py"]) == [], "'manager' is too generic to select anything"
    assert pick(["static/js/client/app.js"]) == [], "'app' is generic: only 'app.js' itself selects"
    assert pick(["tests/test_other.py"]) == ["tests/test_other.py"], "a changed test always runs"
    assert pick([]) == []


def test_a_client_change_always_brings_the_whole_client_guards(tmp_path):
    # Deploy #6: the cache-version and cold-load-budget guards never name the file they police, so a concord.js
    # change found them failing only in the full suite, 50 minutes in.
    files = _tree(tmp_path, {
        "tests/test_a_client_change_bumps_the_cache.py": "HEAD vs sw.js",
        "tests/test_client_boot_payload_budget.py": "cold load bytes",
        "tests/test_server_only.py": "unrelated",
    })
    pick = lambda paths: G["affected_test_files"](str(tmp_path), paths, files)
    guards = ["tests/test_a_client_change_bumps_the_cache.py", "tests/test_client_boot_payload_budget.py"]
    assert pick(["static/js/client/concord.js"]) == guards
    assert pick(["templates/client.html"]) == guards
    assert pick(["app/services/chunked_upload.py"]) == [], "a server-only change does not need them"


def test_preflight_runs_only_when_it_is_a_shortcut(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setitem(G["run_preflight"].__globals__, "run_full_suite",
                        lambda root, env, d, jobs=None, skip_browser=False, select=None: (ran.append(select), (True, "ok"))[1])
    files = ["tests/test_%d.py" % i for i in range(10)]
    monkeypatch.setitem(G["run_preflight"].__globals__, "discover_test_files", lambda root: files)
    for changed, sel, want_ran in ((None, [], False), ([], [], False), (["x.js"], [], False),
                                   (["x.js"], files[:2], True), (["x.js"], files[:6], False)):
        ran.clear()
        monkeypatch.setitem(G["run_preflight"].__globals__, "deploy_changes", lambda root, c=changed: c)
        monkeypatch.setitem(G["run_preflight"].__globals__, "affected_test_files", lambda root, p, f, s=sel: s)
        did, ok, msg = G["run_preflight"]("r", {}, "d")
        assert did is want_ran and ok, (changed, sel, msg)
        assert bool(ran) is want_ran
    monkeypatch.setitem(G["run_preflight"].__globals__, "run_full_suite", lambda *a, **k: (False, "1 failing"))
    monkeypatch.setitem(G["run_preflight"].__globals__, "affected_test_files", lambda root, p, f: files[:2])
    monkeypatch.setitem(G["run_preflight"].__globals__, "deploy_changes", lambda root: ["x.js"])
    assert G["run_preflight"]("r", {}, "d")[:2] == (True, False)


def test_a_failing_preflight_stops_the_gate_before_the_full_run():
    src = (ROOT / "scripts/deploy_regression_gate.py").read_text()
    gate = src[src.index("def run_gate("):]
    pre, req = gate.index("PC_GATE_NO_PREFLIGHT"), gate.index("required = TESTS")
    assert pre < req, "the preflight must run before the required pass and the full suite"
    block = gate[pre:req]
    assert "if ran and not ok:" in block and "return 1" in block
    assert "PC_GATE_NO_PREFLIGHT" in block, "an escape hatch exists, and is off by default"
    assert "'--changed'" in src, "the preflight is also runnable on its own"
