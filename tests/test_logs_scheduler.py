"""Tests for the agentic system-health report (app/services/logs_scheduler.py).

Run: venv/bin/python -m unittest tests.test_logs_scheduler

These cover the orchestration/formatting that replaced the old hardcoded log collector:
node selection, per-node agent fan-out, the disabled/empty guards, and that the report is
persisted to the Logs conversation. node_service.run_agent (the LLM tool loop) and the DB are
mocked, so the tests are fast and need no model, SSH, or database.
"""
import asyncio
import unittest
from unittest import mock

from app.services import logs_scheduler as L


def _run(coro):
    return asyncio.run(coro)


class TestTelegramMarkdown(unittest.TestCase):
    def test_headings_and_bold_collapse_to_v1(self):
        out = L._to_telegram_markdown("## 🩺 Report\nsome **bold** text")
        self.assertIn("*🩺 Report*", out)        # ## heading -> *bold*
        self.assertIn("*bold*", out)              # **bold** -> *bold*
        self.assertNotIn("**", out)

    def test_indentation_stripped(self):
        out = L._to_telegram_markdown("    indented line")
        self.assertEqual(out, "indented line")


class TestRenderBoard(unittest.TestCase):
    def test_renders_six_emoji_lines_in_canonical_order(self):
        raw = ("smart|green|sda,sdb PASSED\n"
               "disk|green|/ 33%, /raid 63%\n"
               "raid|green|md0 [UUU]\n"
               "services|green|none failed\n"
               "swap|none|no swap\n"
               "errors|green|none")
        board = L._render_board(raw)
        lines = board.splitlines()
        # canonical order regardless of input order, deterministic icons + status emojis
        self.assertEqual(lines[0], "💾 Disk: 🟢 / 33%, /raid 63%")
        self.assertEqual(lines[1], "🔧 SMART: 🟢 sda,sdb PASSED")
        self.assertEqual(lines[2], "💿 RAID: 🟢 md0 [UUU]")
        self.assertEqual(lines[3], "⚙️ Services: 🟢 none failed")
        self.assertEqual(lines[4], "🔄 Swap: ⚪ no swap")
        self.assertEqual(lines[5], "📜 Errors (6h): 🟢 none")

    def test_status_and_key_fuzzy_match(self):
        # model embellishes the status word / key; we still map it
        raw = ("Disk usage|GREEN|ok\nSMART|red|sdb FAILED\nraid array|none|no md\n"
               "services|yellow|1 degraded\nswap|green|0 used\nerrors (6h)|red|3 oom")
        board = L._render_board(raw)
        self.assertIn("🔧 SMART: 🔴 sdb FAILED", board)
        self.assertIn("⚙️ Services: 🟡 1 degraded", board)
        self.assertIn("📜 Errors (6h): 🔴 3 oom", board)

    def test_too_few_rows_returns_none_for_fallback(self):
        self.assertIsNone(L._render_board("disk|green|ok\nnonsense line"))
        self.assertIsNone(L._render_board(""))


class TestErrorSamples(unittest.TestCase):
    """The evidence lines under the Errors row. A count alone ('11 journal + 8 dmesg errors') never
    said WHAT was wrong, which is the whole reason these exist."""

    RAW = ("== journal ==\n"
           "12\tkernel: ata3.00: failed command: READ FPDMA QUEUED\n"
           "3\tnginx: upstream timed out while reading response header\n"
           "== dmesg ==\n"
           "4\tata3: SATA link down (SStatus 0 SControl 330)\n")

    def test_counts_sources_and_verbatim_text(self):
        out = L._parse_error_samples(self.RAW)
        self.assertEqual(out[0], "↳ ×12 kernel: ata3.00: failed command: READ FPDMA QUEUED")
        self.assertEqual(out[1], "↳ ×3 nginx: upstream timed out while reading response header")
        # dmesg lines carry no unit prefix of their own, so the source is labelled
        self.assertEqual(out[2], "↳ ×4 dmesg: ata3: SATA link down (SStatus 0 SControl 330)")

    def test_ignores_noise_and_caps_the_list(self):
        self.assertEqual(L._parse_error_samples("== journal ==\nnot a count row\n"), [])
        self.assertEqual(L._parse_error_samples(""), [])
        many = "== journal ==\n" + "".join(f"{i}\tline {i}\n" for i in range(20))
        self.assertEqual(len(L._parse_error_samples(many)), L._ERROR_SAMPLE_MAX)

    def test_strips_chars_that_break_telegram_markdown(self):
        out = L._parse_error_samples("== journal ==\n1\tkernel: *** `oops` in foo_bar.service\n")
        self.assertEqual(out, ["↳ ×1 kernel: oops in foo_bar.service"])   # `_` kept: it's in unit names

    def test_attached_under_the_errors_row(self):
        board = L._render_board("disk|green|ok\nsmart|green|ok\nraid|none|no md\n"
                                "services|green|none failed\nswap|green|0 used\nerrors|red|12 errors")
        out = L._with_error_samples(board, L._parse_error_samples(self.RAW)).splitlines()
        self.assertEqual(out[5], "📜 Errors (6h): 🔴 12 errors")
        self.assertTrue(out[6].startswith("↳ ×12 kernel:"))     # evidence sits with the count

    def test_appended_when_there_is_no_errors_row(self):
        # agent leg failed -> body is an error string, but the raw evidence is still worth showing
        out = L._with_error_samples("⚠️ agent error: boom", L._parse_error_samples(self.RAW))
        self.assertTrue(out.startswith("⚠️ agent error: boom\n↳ ×12 kernel:"))

    def test_no_samples_leaves_the_board_untouched(self):
        self.assertEqual(L._with_error_samples("💾 Disk: 🟢 ok", []), "💾 Disk: 🟢 ok")


class TestSelectedNodes(unittest.TestCase):
    def test_default_includes_local_plus_all_configured(self):
        with mock.patch.object(L.node_service, "get_nodes", return_value={"nas": "u@nas"}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": []}):
            nodes = L.selected_nodes(db=object())
        self.assertEqual(nodes, {"local": "local", "nas": "u@nas"})

    def test_logs_nodes_narrows_selection_and_ignores_unknown(self):
        with mock.patch.object(L.node_service, "get_nodes", return_value={"nas": "u@nas", "srv": "u@srv"}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": ["nas", "ghost"]}):
            nodes = L.selected_nodes(db=object())
        self.assertEqual(nodes, {"nas": "u@nas"})  # "ghost" isn't configured -> dropped


class TestBuildHealthReport(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_node_exec_returns_guard_message(self):
        with mock.patch.object(L.node_service, "is_enabled", return_value=False), \
             mock.patch.object(L.node_service, "get_nodes", return_value={}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": []}):
            text = await L.build_health_report(db=object(), admin=mock.Mock())
        self.assertIn("Remote Node Management is disabled", text)
        self.assertIn("🩺 System Health Report", text)

    async def test_runs_agent_once_per_node_and_composes_report(self):
        agent = mock.AsyncMock(side_effect=lambda *a, **k: f"🟢 all good on {a[2]}")
        with mock.patch.object(L.node_service, "is_enabled", return_value=True), \
             mock.patch.object(L.node_service, "get_nodes", return_value={"nas": "u@nas"}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": []}), \
             mock.patch.object(L, "ChatService", return_value=mock.Mock()), \
             mock.patch.object(L.node_service, "run_agent", agent):
            text = await L.build_health_report(db=object(), admin=mock.Mock())

        # local + nas -> two agent invocations, each in report_mode
        self.assertEqual(agent.await_count, 2)
        for call in agent.await_args_list:
            self.assertTrue(call.kwargs.get("report_mode"))
        self.assertIn("🖥️ *local*", text)
        self.assertIn("🖥️ *nas*", text)
        self.assertIn("all good on local", text)
        self.assertIn("all good on nas", text)

    async def test_agent_exception_is_isolated_per_node(self):
        async def boom(db, user, node, *a, **k):
            if node == "nas":
                raise RuntimeError("ssh down")
            return f"🟢 {node} ok"
        with mock.patch.object(L.node_service, "is_enabled", return_value=True), \
             mock.patch.object(L.node_service, "get_nodes", return_value={"nas": "u@nas"}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": []}), \
             mock.patch.object(L, "ChatService", return_value=mock.Mock()), \
             mock.patch.object(L.node_service, "run_agent", side_effect=boom):
            text = await L.build_health_report(db=object(), admin=mock.Mock())
        self.assertIn("⚠️ agent error: ssh down", text)  # nas failed
        self.assertIn("local ok", text)                   # local still reported


class TestRunLogsForAdmin(unittest.IsolatedAsyncioTestCase):
    async def test_persists_report_and_returns_text(self):
        admin = mock.Mock(id=1, telegram_enabled=False, telegram_chat_id=None)
        fake_db = mock.Mock()
        fake_db.query.return_value.filter.return_value.first.return_value = admin

        with mock.patch.object(L, "SessionLocal", return_value=fake_db), \
             mock.patch.object(L, "build_health_report", mock.AsyncMock(return_value="REPORT")), \
             mock.patch.object(L, "get_or_create_logs_chat", return_value=mock.Mock(id=7)):
            text = await L.run_logs_for_admin(return_text=True)

        self.assertEqual(text, "REPORT")
        self.assertTrue(fake_db.add.called)        # a Message was added
        self.assertTrue(fake_db.commit.called)
        self.assertTrue(fake_db.close.called)

    async def test_no_admin_returns_none(self):
        fake_db = mock.Mock()
        fake_db.query.return_value.filter.return_value.first.return_value = None
        with mock.patch.object(L, "SessionLocal", return_value=fake_db):
            text = await L.run_logs_for_admin(return_text=True)
        self.assertIsNone(text)


if __name__ == "__main__":
    unittest.main()
