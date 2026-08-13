"""The bound on a single command inside `node agent`, and why it is the size it is.

Run: venv-unified/bin/python -m pytest tests/test_node_agent_step_timeout.py -q

This exists because of a failure that produced NOTHING to look at. The documented way to check a
node is to ask the agent to run `./test.sh --brief`; the suite MEASURES 10m22s (622s) and the step
timeout defaulted to 600s, so the command was killed 22 seconds from the finish line. `--brief`
prints one block at the very end and nothing before it, so the agent's captured output was empty —
ten minutes of apparently nothing, then nothing, with an idle GPU throughout (the model only runs
between steps). Confirmed from the production log, where job #2 started 21:50:27 and failed
22:00:27, `exit=None`: exactly 600 seconds.

Three things are pinned here, each of which was wrong or unreachable:

  * the default must exceed the longest command this repo asks an agent to run;
  * a BLANK stored value must resolve to that default (it was blank in production — `float('')`
    raises, and only the except branch saved it);
  * the setting must have an input in Admin → Nodes. It had none, so the operator's obvious fix —
    "raise the timeout" — could not be made from the panel at all, which is how a 22-second miss
    turns into an unexplained dead end.
"""
import re
import unittest
from pathlib import Path
from unittest import mock

from app.schemas import SettingsResponse
from app.services import node_service as N

_REPO = Path(__file__).resolve().parents[1]
_NODES_TAB = _REPO / "templates" / "admin" / "tabs" / "nodes.html"

# What the suite actually took, measured on server1 with `time ./test.sh --brief`. Not a guess and
# not a budget — if the suite grows past this, the number to change is the DEFAULT, not this line.
MEASURED_SUITE_SECONDS = 622


class DefaultTests(unittest.TestCase):
    def test_the_default_outlasts_the_check_suite(self):
        default = float(SettingsResponse.model_fields["node_exec_agent_step_timeout"].default)
        self.assertGreater(
            default, MEASURED_SUITE_SECONDS,
            "the agent kills a command at this bound, and the suite it is documented to run takes "
            f"{MEASURED_SUITE_SECONDS}s — a default below that returns an EMPTY report, because "
            "--brief prints nothing until the end")

    def test_a_blank_stored_value_resolves_to_the_default(self):
        """Blank is what was actually stored in production — a setting declared in SettingsResponse
        with no input in any tab never hydrates, and Save writes the empty value back."""
        default = float(SettingsResponse.model_fields["node_exec_agent_step_timeout"].default)
        for stored in ("", "   ", "not-a-number"):
            with mock.patch.object(N.settings_store, "get",
                                   side_effect=lambda k, _v=stored: _v if "step_timeout" in k else None):
                self.assertEqual(N._agent_step_timeout(mock.MagicMock()), default,
                                 f"stored {stored!r} must fall back to the declared default")

    def test_zero_means_use_the_job_timeout(self):
        """0 is a deliberate admin choice ("bound it like any other job"), not a missing value."""
        with mock.patch.object(N.settings_store, "get",
                               side_effect=lambda k: "0" if "step_timeout" in k else None), \
             mock.patch.object(N, "_job_timeout", return_value=None) as job_timeout:
            self.assertIsNone(N._agent_step_timeout(mock.MagicMock()))
            job_timeout.assert_called_once()


class AdminFieldTests(unittest.TestCase):
    def test_the_timeout_is_editable_from_the_admin_panel(self):
        """The fix has to be reachable by the person hitting the problem. Without a field, raising
        this needs knowledge of a key name that appears in no UI."""
        html = _NODES_TAB.read_text(encoding="utf-8")
        self.assertIn('id="node_exec_agent_step_timeout"', html)
        self.assertIn('name="node_exec_agent_step_timeout"', html)

    def test_the_field_is_wired_the_way_admin_js_reads_it(self):
        """admin.js hydrates by element id and saves by name; a mismatch loads blank forever and
        then posts the blank back over the stored value."""
        html = _NODES_TAB.read_text(encoding="utf-8")
        tag = re.search(r'<input[^>]*node_exec_agent_step_timeout[^>]*>', html)
        self.assertIsNotNone(tag, "the input must exist")
        ids = re.findall(r'\bid="([^"]+)"', tag.group(0))
        names = re.findall(r'\bname="([^"]+)"', tag.group(0))
        self.assertEqual(ids, names, "id and name must be the same key")



class StepProgressTests(unittest.IsolatedAsyncioTestCase):
    """A long step has to SAY it is alive.

    The agent notifies on completed steps only, so a ten-minute command showed the user nothing for
    ten minutes — and silence is indistinguishable from a hang. The documented check-suite run was
    reported as "hung at the git clone step" twice and cancelled both times, while it was working
    normally (the clone had finished in 5s; pytest was running). Nothing was broken except what the
    user could see.
    """

    async def test_a_long_step_pings_while_it_runs(self):
        pings = []

        async def notify(msg):
            pings.append(str(msg))

        with mock.patch.object(N, "_STEP_PING_SECONDS", 0.3):
            job = await N._run_step_with_progress(
                mock.MagicMock(), "local", "local",
                "echo first-line; sleep 1.2; echo done", mock.MagicMock(id=1), notify, 30)

        self.assertEqual(job.status, "done", job.output)
        self.assertTrue(pings, "a step that outlives the ping interval must report that it is alive")
        self.assertTrue(any("still running" in p for p in pings), pings)
        # The ping carries the command and the last thing it printed — which for the check suite is
        # checkall's per-suite stderr heartbeat, the only progress that exists before the final block.
        self.assertTrue(any("first-line" in p for p in pings),
                        f"the ping should show the newest output line: {pings}")

    async def test_a_fast_step_pings_nothing(self):
        """A one-second command must not grow a progress log."""
        pings = []

        async def notify(msg):
            pings.append(str(msg))

        with mock.patch.object(N, "_STEP_PING_SECONDS", 5.0):
            job = await N._run_step_with_progress(
                mock.MagicMock(), "local", "local", "echo quick", mock.MagicMock(id=1), notify, 30)
        self.assertEqual(job.status, "done")
        self.assertEqual(pings, [])

    async def test_a_failing_notify_never_kills_the_step(self):
        async def notify(_msg):
            raise RuntimeError("channel gone")

        with mock.patch.object(N, "_STEP_PING_SECONDS", 0.3):
            job = await N._run_step_with_progress(
                mock.MagicMock(), "local", "local", "sleep 1; echo survived",
                mock.MagicMock(id=1), notify, 30)
        self.assertEqual(job.status, "done")
        self.assertIn("survived", job.output)

if __name__ == "__main__":
    unittest.main()
