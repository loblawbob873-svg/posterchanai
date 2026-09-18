"""THE DEPLOY GATE MUST BE ABLE TO RUN ON A BOX WHOSE MEMORY IS ALREADY COMMITTED.

Measured 2026-09-18 on this machine: `./sync.sh` was killed twice for low memory before its first
shard finished. 62 GB of RAM, ~26 GB of it actually available — the rest committed to Postgres's
shared buffers — against `_default_jobs()`, which sizes the shard count by CPU alone and knows
nothing about memory. Every shard is a fresh `pytest` that imports the whole application.

WHY THIS IS A CORRECTNESS PROBLEM AND NOT A CONVENIENCE ONE. `sync.sh` gates on the whole suite
because a hand-typed list once let unlisted tests fail against shipped code for days. A gate that
cannot fit in the memory of the box the code is on is a gate somebody works around — by pushing
past it, which is precisely the failure the full-suite gate exists to prevent. Fewer shards and a
longer wait is the honest answer; no gate is not.

`PC_GATE_JOBS` (and `--jobs`) therefore only ever changes HOW MANY tests run at once. It cannot
change WHICH tests run, and it cannot turn the gate off — a "0 shards" or a garbage value means
"decide by CPU", the behaviour that shipped before.
"""
import os
import runpy
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts/deploy_regression_gate.py"
SRC = GATE.read_text(encoding="utf-8")


def _module():
    argv = sys.argv
    sys.argv = ["deploy_regression_gate.py"]
    try:
        return runpy.run_path(str(GATE), run_name="_test_probe")
    finally:
        sys.argv = argv


class TheShardCountIsAskable(unittest.TestCase):
    def setUp(self):
        self.mod = _module()
        self.prev = os.environ.get("PC_GATE_JOBS")

    def tearDown(self):
        if self.prev is None:
            os.environ.pop("PC_GATE_JOBS", None)
        else:
            os.environ["PC_GATE_JOBS"] = self.prev

    def jobs(self, value):
        if value is None:
            os.environ.pop("PC_GATE_JOBS", None)
        else:
            os.environ["PC_GATE_JOBS"] = value
        return self.mod["_env_jobs"]()

    def test_a_number_is_honoured(self):
        self.assertEqual(2, self.jobs("2"))

    def test_unset_means_decide_by_cpu(self):
        self.assertEqual(0, self.jobs(None),
                         "0 is the sentinel for 'decide by CPU' — the behaviour that shipped before")

    def test_nonsense_never_silently_disables_anything(self):
        for bad in ("", "abc", "-3", "2.5", " "):
            with self.subTest(value=bad):
                self.assertEqual(0, self.jobs(bad))

    def test_an_absurd_number_is_capped_rather_than_obeyed(self):
        self.assertEqual(64, self.jobs("100000"),
                         "a typo must not fork ten thousand pytest processes")

    def test_the_option_reaches_the_suite_and_not_just_the_parser(self):
        """An accepted flag that changes nothing is worse than no flag."""
        self.assertIn("run_full_suite(root, env, directory, jobs=jobs)", SRC,
                      "run_gate accepts a shard count and drops it on the floor")
        self.assertIn("def run_gate(root=ROOT, receipt=None, full=False, jobs=0):", SRC)
        self.assertRegex(SRC, r"parser\.add_argument\('--jobs'")


class ItCannotWeakenTheGate(unittest.TestCase):
    """The one thing this option must never become is a way past the suite."""

    def test_it_does_not_touch_which_tests_are_discovered(self):
        disc = SRC[SRC.index("def discover_test_files("):]
        disc = disc[:disc.index("\ndef ")]
        for name in ("PC_GATE_JOBS", "_env_jobs", "jobs"):
            self.assertNotIn(name, disc,
                             "the shard count leaked into test DISCOVERY — it may change how many "
                             "tests run at once, never which ones run")

    def test_the_required_list_is_not_shardable_away(self):
        self.assertIn("--full", SRC)
        # plan_shards only ever partitions the files it is given; it must not drop any.
        plan = SRC[SRC.index("def plan_shards("):]
        plan = plan[:plan.index("\ndef ")]
        self.assertNotIn("[:", plan.split("return")[0].replace("files[", "F["),
                         "plan_shards truncates its input somewhere — a shard count would then "
                         "decide which tests are skipped")

    def test_one_shard_still_runs_every_discovered_file(self):
        """RUN the planner: with jobs=1 every file must still be in the plan, exactly once."""
        mod = _module()
        files = [f"tests/test_{i}.py" for i in range(37)]
        for jobs in (1, 2, 6):
            with self.subTest(jobs=jobs):
                shards = mod["plan_shards"](files, jobs, {}, ROOT)
                flat = [f for shard in shards for f in shard]
                self.assertCountEqual(files, flat,
                                      f"with {jobs} shard(s) the plan lost or duplicated files")


class TheHelpSaysSo(unittest.TestCase):
    def test_the_flag_is_documented_where_somebody_would_look(self):
        r = subprocess.run([sys.executable, str(GATE), "--help"],
                           capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(0, r.returncode, r.stderr[-400:])
        self.assertIn("--jobs", r.stdout)
        self.assertIn("PC_GATE_JOBS", r.stdout,
                      "the environment variable is the form a deploy script uses, and --help is "
                      "the only place anybody will find it")


if __name__ == "__main__":
    unittest.main()
