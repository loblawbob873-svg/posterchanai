"""Tests for the agentic system-health report (app/services/logs_scheduler.py).

Run: venv/bin/python -m unittest tests.test_logs_scheduler

These cover the orchestration/formatting that replaced the old hardcoded log collector:
node selection, per-node agent fan-out, the disabled/empty guards, and that the report is
persisted to the Logs conversation. node_service.run_agent (the LLM tool loop) and the DB are
mocked, so the tests are fast and need no model, SSH, or database.
"""
import asyncio
import re
import shutil
import subprocess
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


class TestProbeFactCheck(unittest.TestCase):
    """The measured rows that override the model's. Every case here is a real report this produced:
    a green dot beside the word 'degraded', an invented fourth disk, an invented /raid mount, swap
    on a host with none, a drive silently dropped, and 'no RAID array' over a healthy one."""

    # Verbatim _HEALTH_SHELL output from the host whose report claimed 'RAID 🟢 degraded (disk 4
    # failed)', '/ 33%, /raid 63%', 'sda,sdb,nvme PASSED' and 'Swap 🟢 2048M available'.
    RAW = (
        "== disk ==\n"
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "/dev/dm-0       930G  403G  521G  44% /\n"
        "/dev/nvme0n1p1  2.0G  852M  1.2G  43% /boot\n"
        "/dev/md0         11T  9.1T  1.9T  84% /usb\n"
        "\n== smart ==\n"
        "sda: SMART overall-health self-assessment test result: PASSED\n"
        "sdb: SMART overall-health self-assessment test result: PASSED\n"
        "sdc: SMART overall-health self-assessment test result: PASSED\n"
        "nvme0n1: SMART overall-health self-assessment test result: PASSED\n"
        "\n== raid ==\n"
        "Personalities : [raid4] [raid5] [raid6] \n"
        "md0 : active raid5 sdc1[3] sda1[0] sdb1[1]\n"
        "      11720777728 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/3] [UUU]\n"
        "      bitmap: 0/44 pages [0KB], 65536KB chunk\n"
        "\nunused devices: <none>\n"
        "\n== failed systemd units ==\n"
        "none failed\n"
        "\n== swap ==\n"
        "               total        used        free      shared  buff/cache   available\n"
        "Swap:             0B          0B          0B\n"
        "\n== journal errors 6h ==\n"
        "total: 0 error lines\n"
        "\n== dmesg warn/err ==\n"
    )

    def test_real_host_output_yields_only_true_rows(self):
        rows = L._parse_probe(self.RAW)
        # the array is [3/3] [UUU] — healthy, and there is no fourth disk to have failed
        self.assertEqual(rows["raid"], ("green", "md0 raid5 clean [3/3]"))
        # every drive present, including the one the model dropped
        self.assertEqual(rows["smart"], ("green", "sda,sdb,sdc,nvme0n1 PASSED"))
        # real mounts and real percentages; no invented /raid
        self.assertEqual(rows["disk"][1], "/ 44%, /boot 43%, /usb 84%")
        self.assertEqual(rows["disk"][0], "yellow")            # 84% is a warning, not green
        # no swap is ⚪ 'none', never a green figure
        self.assertEqual(rows["swap"], ("none", "no swap configured"))
        self.assertEqual(rows["services"], ("green", "none failed"))
        self.assertEqual(rows["errors"], ("green", "no errors"))

    def test_measured_rows_beat_the_model(self):
        model = ("disk|green|/ 33%, /raid 63%\nsmart|green|sda,sdb,nvme PASSED\n"
                 "raid|green|degraded (disk 4 failed)\nservices|green|none failed\n"
                 "swap|green|2048M available\nerrors|green|no errors")
        board = L._render_board(model, L._parse_probe(self.RAW))
        self.assertIn("💿 RAID: 🟢 md0 raid5 clean [3/3]", board)
        self.assertNotIn("disk 4 failed", board)
        self.assertNotIn("/raid 63%", board)
        self.assertNotIn("2048M", board)
        self.assertIn("🔄 Swap: ⚪ no swap configured", board)

    def test_probe_alone_renders_a_board_when_the_model_is_absent(self):
        # a node whose agent leg died still gets the measured facts
        self.assertIn("💿 RAID: 🟢", L._render_board("", L._parse_probe(self.RAW)))

    def test_degraded_array_is_red(self):
        raw = ("== raid ==\n"
               "md0 : active raid5 sda1[0] sdb1[1](F)\n"
               "      11720777728 blocks super 1.2 level 5 [3/2] [UU_]\n")
        self.assertEqual(L._parse_probe(raw)["raid"], ("red", "md0 raid5 DEGRADED [3/2]"))

    def test_rebuilding_array_is_yellow(self):
        raw = ("== raid ==\n"
               "md0 : active raid1 sda1[0] sdb1[1]\n"
               "      976630464 blocks [2/2] [UU]\n"
               "      [===>.................]  recovery = 18.5% (1/9) finish=42.0min\n")
        self.assertEqual(L._parse_probe(raw)["raid"], ("yellow", "md0 raid1 rebuilding [2/2]"))

    def test_zfs_pool_is_a_raid_array(self):
        # the second node reported 'no RAID array configured' over a healthy one
        raw = "== raid ==\nPersonalities : \nunused devices: <none>\n  pool: tank\n state: ONLINE\n"
        self.assertEqual(L._parse_probe(raw)["raid"], ("green", "tank ONLINE"))
        raw_bad = "== raid ==\n  pool: tank\n state: DEGRADED\n"
        self.assertEqual(L._parse_probe(raw_bad)["raid"][0], "red")

    def test_no_array_at_all_is_none_not_green(self):
        raw = "== raid ==\nPersonalities : \nunused devices: <none>\n"
        self.assertEqual(L._parse_probe(raw)["raid"], ("none", "no RAID array"))

    def test_unreadable_drive_is_a_warning_never_a_silent_drop(self):
        raw = ("== smart ==\n"
               "sda: SMART overall-health self-assessment test result: PASSED\n"
               "sdb: Permission denied\n"
               "sdc: SMART overall-health self-assessment test result: FAILED!\n")
        status, detail = L._parse_probe(raw)["smart"]
        self.assertEqual(status, "red")
        self.assertIn("sdc FAILED", detail)
        self.assertIn("sdb no result", detail)
        self.assertIn("sda PASSED", detail)

    def test_swap_in_use_and_full(self):
        def row(line):
            return L._parse_probe(f"== swap ==\n{line}\n")["swap"]
        self.assertEqual(row("Swap:          8.0Gi       1.0Gi       7.0Gi")[0], "green")
        self.assertEqual(row("Swap:          8.0Gi       7.9Gi       0.1Gi")[0], "red")

    def test_failed_units_are_named_and_red(self):
        raw = ("== failed systemd units ==\n"
               "nginx.service loaded failed failed A high performance web server\n"
               "foo.service   loaded failed failed Foo\n")
        status, detail = L._parse_probe(raw)["services"]
        self.assertEqual(status, "red")
        self.assertIn("2 failed", detail)
        self.assertIn("nginx.service", detail)

    def test_failed_units_keep_their_names_past_the_status_bullet(self):
        # systemctl prefixes rows with '●' — naive split()[0] names every failed unit "●"
        raw = ("== failed systemd units ==\n"
               "● nginx.service loaded failed failed A high performance web server\n")
        self.assertEqual(L._parse_probe(raw)["services"], ("red", "1 failed: nginx.service"))

    # (an unreadable journal is covered by TestProbeFalseGreens, which asserts the probe's explicit
    # 'probe-error:' marker rather than sniffing the output for permission strings — see the pair of
    # tests there for why the string-sniffing version was worse than the bug it fixed)

    def test_errors_row_keeps_the_models_wording_but_not_its_status(self):
        raw = ("== journal errors 6h ==\n"
               "     11 nginx: upstream timed out\n"
               "total: 11 error lines\n"
               "== dmesg warn/err ==\n"
               "      8 ata#.##: failed command\n")
        rows = L._parse_probe(raw)
        self.assertEqual(rows["errors"], ("yellow", "11 journal, 8 dmesg"))
        # the model names the sources — that wording survives, its green status does not
        board = L._render_board("disk|green|ok\nsmart|green|ok\nraid|none|no md\n"
                                "services|green|none\nswap|none|no swap\n"
                                "errors|green|nginx upstream timeouts, ata3 I/O errors", rows)
        self.assertIn("📜 Errors (6h): 🟡 nginx upstream timeouts, ata3 I/O errors", board)

    def test_unparseable_probe_leaves_the_model_untouched(self):
        self.assertEqual(L._parse_probe(""), {})
        self.assertEqual(L._parse_probe("total gibberish\n"), {})


class TestProbeFalseGreens(unittest.TestCase):
    """Every case here is a way the MEASURED half could have reported a broken system as healthy —
    the worst outcome for this feature, and worse than the model's invention because it now wins."""

    def test_a_command_that_could_not_run_is_never_green(self):
        # `sudo -n journalctl` with no sudoers rule exits nonzero having printed nothing, which is
        # byte-identical to a clean host; systemctl unable to reach the bus likewise
        errs = L._parse_probe("== journal errors 6h ==\nprobe-error: journalctl exit 1\n"
                              "total: 0 error lines\n")["errors"]
        self.assertEqual(errs[0], "yellow")
        self.assertIn("unreadable", errs[1])
        dmesg = L._parse_probe("== dmesg warn/err ==\nprobe-error: dmesg exit 1\n")["errors"]
        self.assertEqual(dmesg[0], "yellow")
        svc = L._parse_probe("== failed systemd units ==\nprobe-error: systemctl exit 1\n")["services"]
        self.assertEqual(svc, ("yellow", "could not query systemd"))

    def test_real_log_content_is_not_mistaken_for_a_permission_failure(self):
        # 'sudo:' and 'Permission denied' are among the commonest strings IN journal errors; a host
        # with thousands of real ones must not be reported as an unreadable journal
        raw = ("== journal errors 6h ==\n"
               "     31 sudo:   bob : # incorrect password attempts ; TTY=pts/#\n"
               "     12 nginx: [crit] open() failed (#: Permission denied)\n"
               "total: 4312 error lines\n")
        self.assertEqual(L._parse_probe(raw)["errors"], ("yellow", "4312 journal, 0 dmesg"))

    def test_zfs_pool_online_with_unrecoverable_errors_is_not_green(self):
        # ZFS reports the fault on `status:` while `state:` stays ONLINE — reading state alone
        # calls a pool with unrecoverable errors healthy
        raw = ("== raid ==\n  pool: tank\n state: ONLINE\n"
               "status: One or more devices has experienced an unrecoverable error.\n")
        status, detail = L._parse_probe(raw)["raid"]
        self.assertEqual(status, "yellow")
        self.assertIn("unrecoverable", detail)

    def test_the_oldest_array_is_not_truncated_away(self):
        # mdstat lists arrays newest-first, so a tight `head` drops the OLDEST — usually the data
        # array — and reports the survivors as clean. The degraded one must reach the parser.
        lines = ["== raid =="] + [f"md{i} : active raid1 sda{i}[0] sdb{i}[1]\n      976630464 blocks [2/2] [UU]"
                                  for i in range(4, 0, -1)]
        lines += ["md0 : active raid5 sda1[0] sdb1[1]",
                  "      11720777728 blocks super 1.2 level 5 [3/2] [UU_]"]
        status, detail = L._parse_probe("\n".join(lines))["raid"]
        self.assertEqual(status, "red")
        self.assertIn("md0 raid5 DEGRADED [3/2]", detail)
        # …and the probe must not cut those lines off before the parser ever sees them: 5 arrays are
        # ~16 lines of mdstat, so the cap has to leave real headroom
        cap = int(re.search(r"/proc/mdstat[^\n]*head -(\d+)", L._HEALTH_SHELL).group(1))
        self.assertGreaterEqual(cap, 40)

    def test_a_full_disk_past_the_detail_cap_still_sets_the_status(self):
        rows = ["== disk =="] + [f"/dev/x{i}  1T  1T  0  9% /tank/ds{i}" for i in range(11)]
        rows.append("/dev/sdb1  11T  11T  0 100% /usb")
        status, detail = L._parse_probe("\n".join(rows))["disk"]
        self.assertEqual(status, "red")
        self.assertIn("/usb 100%", detail)      # the fullest mounts are the ones shown
        self.assertIn("more", detail)

    def test_a_mount_point_with_a_space_is_not_dropped(self):
        raw = "== disk ==\n/dev/sdb1  11T  11T  0 100% /media/USB Drive\n"
        self.assertEqual(L._parse_probe(raw)["disk"], ("red", "/media/USB Drive 100%"))

    def test_hardware_raid_seen_only_by_the_agent_survives_the_override(self):
        # megaraid/btrfs are invisible to mdstat and zpool, so probe 'none' means "no evidence"
        probe = L._parse_probe("== raid ==\nPersonalities : \nunused devices: <none>\n")
        model = ("disk|green|ok\nsmart|green|ok\nraid|red|LSI MegaRAID vd0 DEGRADED\n"
                 "services|green|none failed\nswap|none|no swap\nerrors|green|none")
        self.assertIn("💿 RAID: 🔴 LSI MegaRAID vd0 DEGRADED", L._render_board(model, probe))

    def test_the_probe_is_a_floor_on_error_severity_not_a_ceiling(self):
        probe = {"errors": ("yellow", "9000 journal, 0 dmesg")}
        board = L._render_board("errors|red|kernel I/O errors on ata3, disk dying", probe)
        self.assertIn("📜 Errors (6h): 🔴", board)
        self.assertIn("disk dying", board)
        self.assertIn("(9000 journal, 0 dmesg)", board)   # measured counts, not the model's

    def test_measured_counts_replace_the_models_numbers(self):
        probe = {"errors": ("yellow", "4312 journal, 88 dmesg")}
        board = L._render_board("errors|yellow|3 nginx timeouts, 1 ata error", probe)
        self.assertIn("(4312 journal, 88 dmesg)", board)

    def test_few_measured_rows_are_still_rendered(self):
        # the agent-error path renders probe-only; the <4 floor is about the MODEL's board
        probe = {"raid": ("red", "md0 raid5 DEGRADED [3/2]"), "smart": ("red", "sdb FAILED")}
        board = L._render_board("", probe)
        self.assertIn("💿 RAID: 🔴 md0 raid5 DEGRADED [3/2]", board)
        self.assertIn("🔧 SMART: 🔴 sdb FAILED", board)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_the_probe_script_really_emits_its_failure_markers(self):
        """Runs the REAL _HEALTH_SHELL with the privileged commands stubbed to fail.

        The marker only works if each leg captures the exit status of the command itself: written as
        `f=$(systemctl … | head -20); rc=$?`, `rc` is HEAD's status, which is always 0, so the marker
        never fires and an unreachable systemd reports 🟢 'none failed'. No parser test can catch
        that — the bug is in the shell, and it was in this file until this test existed."""
        stub = ("mkdir -p /tmp/pcstub && for c in sudo systemctl; do "
                "printf '#!/bin/sh\\nexit 1\\n' > /tmp/pcstub/$c; chmod +x /tmp/pcstub/$c; done\n"
                "export PATH=/tmp/pcstub:$PATH\n")
        out = subprocess.run(["bash", "-c", stub + L._HEALTH_SHELL],
                             capture_output=True, text=True, timeout=120).stdout
        rows = L._parse_probe(out)
        self.assertEqual(rows["services"], ("yellow", "could not query systemd"))
        self.assertEqual(rows["errors"][0], "yellow")
        self.assertIn("unreadable", rows["errors"][1])

    def test_stale_inactive_array_is_a_warning_not_a_permanent_red(self):
        # md127 with an all-spare member is a leftover superblock from a removed disk: not carrying
        # data, not at risk. A DEGRADED array is the one that is live and one failure from loss.
        raw = "== raid ==\nmd127 : inactive sdb1[1](S)\n      976630464 blocks\n"
        self.assertEqual(L._parse_probe(raw)["raid"], ("yellow", "md127 inactive"))


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
    def test_default_passes_through_every_available_node(self):
        with mock.patch.object(L.node_service, "all_nodes", return_value={"local": "local", "nas": "u@nas"}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": []}):
            nodes = L.selected_nodes(db=object())
        # `local` comes FROM node_service.all_nodes now, not from here — an empty
        # `logs_nodes` must simply pass the whole registry through unfiltered.
        self.assertEqual(nodes, {"local": "local", "nas": "u@nas"})

    def test_logs_nodes_narrows_selection_and_ignores_unknown(self):
        with mock.patch.object(L.node_service, "all_nodes", return_value={"nas": "u@nas", "srv": "u@srv"}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": ["nas", "ghost"]}):
            nodes = L.selected_nodes(db=object())
        self.assertEqual(nodes, {"nas": "u@nas"})  # "ghost" isn't configured -> dropped


class TestBuildHealthReport(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_node_exec_returns_guard_message(self):
        with mock.patch.object(L.node_service, "is_enabled", return_value=False), \
             mock.patch.object(L.node_service, "all_nodes", return_value={}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": []}):
            text = await L.build_health_report(db=object(), admin=mock.Mock())
        self.assertIn("Agentic Node Management is disabled", text)
        self.assertIn("🩺 System Health Report", text)

    async def test_runs_agent_once_per_node_and_composes_report(self):
        agent = mock.AsyncMock(side_effect=lambda *a, **k: f"🟢 all good on {a[2]}")
        # The measured probe is mocked out here (it shells out for real on the `local` target, and
        # would then legitimately replace the agent's text with this machine's actual board) — the
        # fact-check itself is covered by TestProbeFactCheck.
        with mock.patch.object(L.node_service, "is_enabled", return_value=True), \
             mock.patch.object(L.node_service, "all_nodes", return_value={"local": "local", "nas": "u@nas"}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": []}), \
             mock.patch.object(L, "ChatService", return_value=mock.Mock()), \
             mock.patch.object(L, "_health_probe", mock.AsyncMock(return_value=({}, ""))), \
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
             mock.patch.object(L.node_service, "all_nodes", return_value={"local": "local", "nas": "u@nas"}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": []}), \
             mock.patch.object(L, "ChatService", return_value=mock.Mock()), \
             mock.patch.object(L, "_health_probe", mock.AsyncMock(return_value=({}, ""))), \
             mock.patch.object(L.node_service, "run_agent", side_effect=boom):
            text = await L.build_health_report(db=object(), admin=mock.Mock())
        self.assertIn("⚠️ agent error: ssh down", text)  # nas failed
        self.assertIn("local ok", text)                   # local still reported

    async def test_measured_board_survives_a_dead_agent(self):
        """The probe is a separate leg from the agent, so a node whose model/SSH died still reports
        its facts — previously that node's whole section was one '⚠️ agent error' line."""
        rows = {"disk": ("green", "/ 12%"), "smart": ("green", "sda PASSED"),
                "raid": ("green", "md0 raid1 clean [2/2]"), "services": ("green", "none failed"),
                "swap": ("none", "no swap configured"), "errors": ("green", "no errors")}
        async def boom(*a, **k):
            raise RuntimeError("ssh down")
        with mock.patch.object(L.node_service, "is_enabled", return_value=True), \
             mock.patch.object(L.node_service, "all_nodes", return_value={"nas": "u@nas"}), \
             mock.patch.object(L, "get_logs_settings", return_value={"schedule": "1", "nodes": []}), \
             mock.patch.object(L, "ChatService", return_value=mock.Mock()), \
             mock.patch.object(L, "_health_probe", mock.AsyncMock(return_value=(rows, ""))), \
             mock.patch.object(L.node_service, "run_agent", side_effect=boom):
            text = await L.build_health_report(db=object(), admin=mock.Mock())
        self.assertIn("💿 RAID: 🟢 md0 raid1 clean [2/2]", text)
        self.assertIn("⚠️ agent error: ssh down", text)


class TestRunLogsForAdmin(unittest.IsolatedAsyncioTestCase):
    async def test_persists_report_and_returns_text(self):
        admin = mock.Mock(id=1, telegram_enabled=False, telegram_chat_id=None)
        fake_db = mock.Mock()
        fake_db.query.return_value.filter.return_value.first.return_value = admin

        # The report is no longer a `Message` row — it goes through chat_history.append as an
        # ENCRYPTED relay event (the nostr-datastore migration). Asserting on db.add still passed as
        # "persisted" for a while after that stopped being true, so assert on the call that actually
        # stores it.
        from app.services import chat_history, chat_store
        append = mock.AsyncMock()
        with mock.patch.object(L, "SessionLocal", return_value=fake_db), \
             mock.patch.object(L, "build_health_report", mock.AsyncMock(return_value="REPORT")), \
             mock.patch.object(chat_history, "append", append), \
             mock.patch.object(chat_store, "mirror_conversation", mock.AsyncMock()), \
             mock.patch.object(L, "get_or_create_logs_chat", return_value=mock.Mock(id=7)):
            text = await L.run_logs_for_admin(return_text=True)

        self.assertEqual(text, "REPORT")
        self.assertTrue(append.called)             # the report was stored
        self.assertEqual(append.call_args[0][4], "REPORT")
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
