import pathlib
import os
import sys
import tempfile
import time

from scripts import checkall


def test_os_back_is_an_explicit_live_check():
    """The OS Back driver consumes real repos/issues and must never hide in the local UI group."""
    jobs = {job["name"]: job for job in checkall.discover()}
    assert jobs["check_os_back"]["registered"] is True
    assert jobs["check_os_back"]["group"] == "live"


def test_public_overlay_comparison_is_an_explicit_live_check():
    """An unpublished package pin cannot equal the currently published overlay."""
    jobs = {job["name"]: job for job in checkall.discover()}
    job = jobs["check_gentoo_overlay"]
    assert job["registered"] is True
    assert job["group"] == "live"
    assert job["live_args"] == []  # Uses the Gentoo host, not the app's URL.


def test_finished_check_does_not_wait_for_a_grandchild_holding_stdout():
    """A browser inherited the old PIPE and froze ./test.sh after pytest had already exited."""
    code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)']); "
        "print('parent finished')"
    )
    with tempfile.TemporaryDirectory() as d:
        started = time.monotonic()
        rc, out = checkall._captured(
            [sys.executable, "-c", code], pathlib.Path(d), None, 5, pathlib.Path(d) / "out.log"
        )
    assert rc == 0
    assert "parent finished" in out
    assert time.monotonic() - started < 2


def test_timeout_stops_the_job_and_reports_it():
    with tempfile.TemporaryDirectory() as d:
        rc, out = checkall._captured(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            pathlib.Path(d), None, 0.1, pathlib.Path(d) / "timeout.log"
        )
    assert rc == 124
    assert "killed after 0.1s" in out


def test_finished_check_reaps_its_browser_process_group(tmp_path):
    """A driver returning after terminate() must not leave Chromium for the next check."""
    pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
        "print('driver returned')"
    )
    rc, out = checkall._captured([sys.executable, "-c", code], tmp_path, None, 5,
                                 tmp_path / "reap.log")
    child = int(pid_file.read_text())
    assert rc == 0 and "driver returned" in out
    for _ in range(50):
        if not pathlib.Path(f"/proc/{child}").exists():
            break
        time.sleep(0.02)
    assert not pathlib.Path(f"/proc/{child}").exists()


def test_reruns_in_one_log_directory_get_distinct_browser_profiles(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(checkall, "_captured",
                        lambda argv, cwd, env, timeout, output: (seen.append(env["PC_CHECK_PROFILE"])
                                                                 or (0, "OK")))
    job = {"name": "check_probe", "path": tmp_path / "probe.py", "group": "ui",
           "secs": 1, "env": {}}
    checkall.run_one(job, None, tmp_path, 3)
    checkall.run_one(job, None, tmp_path, 4)
    assert seen[0] != seen[1]
    assert all(str(tmp_path / "profiles") in profile for profile in seen)
    assert all(str(os.getpid()) in profile for profile in seen)


# ── A RED ROW MUST SAY WHY ────────────────────────────────────────────────────────────────────────
#
# `summarise` quotes the check's own verdict line, which is right — but the common shape here is
# `FAIL:` (or `FAIL  5 problem(s):`) followed by `  - …` bullets, so the row printed
# `✗ check_websearch_rate — FAIL:` and nothing else. That cost three separate re-runs of one check
# to discover what it objected to, and it silenced every check written that way, not just that one.


def test_a_verdict_that_introduces_its_reasons_carries_them():
    out = ("Web Search rate + engine-proxy config\n\nFAIL:\n"
           "  - searxng_proxy_engines is ON but settings.yml has no proxy block\n"
           "  - 3/8 searches took over 5s\n")
    line = checkall.summarise({"out": out})
    assert "no proxy block" in line, line
    assert "3/8 searches took over 5s" in line, line
    assert line.startswith("FAIL:"), line


def test_the_other_common_shape_too():
    line = checkall.summarise({"out": "FAIL  5 problem(s):\n  - one thing\n  - another\n"})
    assert "one thing" in line and "another" in line, line


def test_a_verdict_that_is_already_a_sentence_is_left_alone():
    """Quoting the check is the rule; this only adds what a trailing colon promised."""
    assert checkall.summarise({"out": "OK  all icon checks passed\n"}) == "OK  all icon checks passed"


def test_a_pytest_summary_still_wins():
    out = "FAIL:\n  - ignored\n\n=== 3 failed, 2 passed in 1.0s ===\n"
    assert "3 failed" in checkall.summarise({"out": out})


def test_the_reason_is_bounded():
    """It is one row in a table, not the check's whole output."""
    out = "FAIL:\n" + "".join(f"  - problem number {i} with a long description\n" for i in range(80))
    assert len(checkall.summarise({"out": out})) <= 400


def test_the_suites_never_read_or_write_a_bytecode_cache(tmp_path, monkeypatch):
    """Run a real import with stale bytecode whose size and timestamp still match the source."""
    import py_compile
    module = tmp_path / "cached_probe.py"
    module.write_text("value = 'old'\n")
    cached = pathlib.Path(py_compile.compile(str(module), doraise=True))
    original_cache = cached.read_bytes()
    stamp = module.stat()
    module.write_text("value = 'new'\n")
    os.utime(module, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    monkeypatch.setattr(checkall, "ROOT", tmp_path)
    monkeypatch.setattr(checkall, "PY", sys.executable)
    suite = dict(name="probe", group="unit", secs=10,
                 argv=["-c", "import sys, cached_probe; assert sys.dont_write_bytecode; "
                       "assert cached_probe.value == 'new', cached_probe.value"])
    result = checkall.run_suite(suite, tmp_path / "logs")
    assert result["code"] == 0, result["out"]
    assert cached.read_bytes() == original_cache, "the runner rewrote the checkout cache"
    assert not list((tmp_path / "logs").rglob("*.pyc")), "the runner wrote new bytecode"
