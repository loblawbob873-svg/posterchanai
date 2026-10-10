"""./test.sh runs tests/ and tests/client/ through the deploy gate's SHARDED runner, never one serial pytest.

2026-10-10: serially, tests/ took 36 min and tests/client ran past test.sh's 45-min cap without finishing, so the
board could never come back green; and one serial process let a stub one test left in sys.modules fail twelve
tests in another file, failures the sharded gate never saw. The two must be the same runner, so they cannot
disagree about whether the code is good.
"""
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gate():
    return runpy.run_path(str(ROOT / "scripts/deploy_regression_gate.py"))


def test_the_two_halves_split_every_test_file_exactly_once():
    g = _gate()
    files = g["discover_test_files"](ROOT)
    unit = [f for f in files if g["SUITES"]["tests"](f)]
    client = [f for f in files if g["SUITES"]["tests/client"](f)]
    assert unit and client
    assert not set(unit) & set(client), "a file runs in both halves"
    assert set(unit) | set(client) == set(files), "a test file runs in neither half"
    assert all(f.startswith("tests/client/") for f in client)


def test_test_sh_runs_both_halves_through_the_sharded_runner():
    suites = runpy.run_path(str(ROOT / "scripts/checkall.py"))["SUITES"]
    by_name = {s["name"]: s for s in suites}
    for name in ("tests", "tests/client"):
        argv = by_name[name]["argv"]
        assert "pytest" not in argv, "%s runs as one serial pytest again" % name
        assert argv[:3] == ["scripts/deploy_regression_gate.py", "--suite", name], argv
        # test.sh holds the checkout's runner lock; a suite that asked for it again would report itself busy
        assert by_name[name].get("env", {}).get("PC_GATE_MANAGED_PROCESSES") == "1"


def test_a_suite_prints_the_summary_line_the_board_quotes(tmp_path, monkeypatch):
    g = runpy.run_path(str(ROOT / "scripts/deploy_regression_gate.py"), run_name="gate_under_test")
    calls = []

    def fake_full(root, env, directory, jobs=None, skip_browser=False, select=None):
        calls.append(select)
        return True, "42 cases across the full suite in 1s"
    g["run_suite"].__globals__["run_full_suite"] = fake_full
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    with redirect_stdout(out):
        code = g["run_suite"]("tests/client")
    assert code == 0 and "42 passed" in out.getvalue(), out.getvalue()
    assert calls and calls[0]("tests/client/test_x.py") and not calls[0]("tests/test_x.py")
