#!/usr/bin/env python3
"""Run the required desktop regression checks before pushing or publishing a build.

Uses isolated browser profiles, Electron sessions and fake filesystem/network adapters.
A skipped test is missing coverage, so it blocks this gate just like a failed test.

`--full` (what sync.sh runs) then runs EVERY discovered test under tests/, sharded across parallel
pytest processes. The required list alone let tests nobody had listed rot: four were found failing
for days against shipped code (a notification pin since Sep 14 among them) while every deploy
passed. Skips are allowed in the full pass (hardware- and tool-dependent tests skip honestly);
failures, errors and collection errors are not.
"""
from pathlib import Path
import argparse
import concurrent.futures
import hashlib
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
# Inputs exercised by this gate. The overlay updater may legitimately change its
# package pin after testing; it does not change any of these client/test inputs.
INPUTS = ('os/gentoo.sh', 'app', 'static', 'desktop', 'mobile', 'templates', 'scripts', 'tests',
          'os/overlay/gui-libs/posterchan-wayfire-shell',
          'os/overlay/app-misc/posterchanos-shell',
          '.github/workflows/desktop.yml', '.github/workflows/android.yml',
          '.github/workflows/android-emulator.yml', 'sync.sh')
TESTS = (
    'tests/test_deploy_regression_gate.py',
    'tests/test_desktop_workflow_test_triggers.py',
    'tests/test_desktop_overlay_metadata_followup.py',
    'tests/test_deploy_process_cleanup.py',
    'tests/client/test_node_timer_scope.py',
    'tests/test_sync_publish_failures.py',
    'tests/test_sync_nas_fetch_retry.py',
    'tests/test_wayfire_pointer_confinement_runtime.py::test_the_option_is_declared_on_and_the_package_that_carries_it_is_required',
    'tests/client/test_sms_image_paste_full_app.py',
    'tests/client/test_sms_search_focus_full_app.py',
    'tests/client/test_sms_archive_pagination.py',
    'tests/client/test_relay_archive_pages.py',
    'tests/client/test_contacts_name_refresh.py',
    'tests/client/test_contacts_phonebook_guard.py',
    'tests/client/test_contacts_work_offline.py',
    'tests/test_mail_global_search.py',
    'tests/client/test_mail_global_search_full_app.py',
    'tests/test_desktop_clipboard_async.py',
    'tests/test_desktop_clipboard_electron.py',
    'tests/test_android_icon_sprite.py',
    'tests/test_android_sms_history_delivery.py',
    'tests/test_android_sms_archive_checkpoint.py',
    'tests/test_android_sms_archive_cursor.py',
    'tests/test_android_sms_archive_provider.py',
    'tests/test_android_sms_sweep.py',
    'tests/test_android_signer_service_compiles.py',
    'tests/test_android_launcher_touch.py',
    'tests/test_android_sms_image_paste.py',
    'tests/test_android_mms_draft_copy_ownership.py',
    'tests/test_android_mms_receiver_lifecycle.py',
    'tests/test_android_mms_result_mapping.py',
    'tests/test_android_mms_retry_runtime.py',
    'tests/test_android_sms_share_runtime.py',
    'tests/test_android_sms_share_caption.py',
    'tests/test_android_composer_belongs_to_the_conversation.py',
    'tests/test_android_instrumented_evidence.py',
    'tests/test_android_publish_gate.py',
    'tests/test_remote_control_native.py',
    'tests/test_remote_desktop_audio_runtime.py',
    'tests/test_desktop_display_audio.py',
    'tests/test_desktop_linux_loopback_runtime.py',
    'tests/client/test_remote_desktop_mobile_full_app.py',
    'tests/test_remote_desktop_configuration_runtime.py',
    'tests/test_remote_desktop_alignment_runtime.py',
    'tests/test_remote_desktop_start_races.py',
    'tests/test_livecd_kernel_selection_runtime.py',
    'tests/test_livecd_build_artifact_excludes.py',
    'tests/test_installed_welcome_observer.py',
    'tests/test_livecd_welcome_gate.py',
    'tests/test_wayfire_session_packaging.py::test_virtual_gpu_fallback_exports_legacy_drm_only_for_virtio',
    'tests/test_stats_monero_zaps.py',
    'tests/test_media_cached_admission.py',
    'tests/test_media_proxy_diagnostics.py',
    'tests/test_media_transcode_stays_on_the_gpu.py',
    'tests/test_monero_user_history.py',
    'tests/client/test_monero_user_history_render.py',
    'tests/client/test_monero_history_full_app.py',
    'tests/client/test_monero_user_probe_races.py',
    'tests/client/test_monero_user_send_runtime.py::test_pending_checkbox_cannot_resubmit_payment',
    'tests/client/test_monero_user_send_runtime.py::test_uncertain_response_is_terminal_even_after_checkbox_toggle',
    'tests/client/test_monero_user_send_runtime.py::test_valid_receipt_success_and_exact_decimal_payload',
    'tests/client/test_monero_user_send_runtime.py::test_confirmation_discloses_service_deduction_and_extra_network_fee',
    'tests/client/test_quiet_monero_zap_runtime.py::test_success_after_account_switch_never_announces_under_new_account',
    'tests/client/test_quiet_monero_zap_runtime.py::test_withdrawal_unknown_preserves_existing_status_without_tip_checkbox',
    'tests/client/test_monero_paints_what_it_already_read.py',
    'tests/client/test_cord_spec_compat.py',
    'tests/client/test_stats_monero_full_app.py',
    'tests/client/test_user_autocomplete_full_app.py',
    'tests/client/test_concord_channel_controls_full_app.py',
    'tests/test_reminder_history_window.py',
    'tests/test_reminder_notifications.py',
    'tests/client/test_reminder_notifications_runtime.py',
    'tests/client/test_reminder_cache_expiry.py',
    'tests/client/test_notification_author_route_full_app.py',
    'tests/client/test_desktop_notification_history_full_app.py',
    'tests/client/test_android_early_launcher_full_app.py',
    'tests/test_calendar_move_api.py',
    'tests/client/test_calendar_default_and_move_full_app.py',
    'tests/client/test_files_follow_theme.py',
    'tests/client/test_browser_startup_diagnostics.py',
    'tests/client/test_browser_command_diagnostics.py',
    'tests/client/test_browser_devtools_readiness.py',
    'tests/client/test_desktop_offline_full_app.py::test_failed_browser_check_still_closes_chrome_before_removing_profile',
    'tests/client/test_desktop_offline_full_app.py::test_start_keyboard_result_survives_refresh_and_has_visible_focus',
    'tests/client/test_saved_theme_reaches_open_files.py',
    'tests/client/test_dm_delivery.py',
    'tests/client/test_sms_live_notifications.py',
    'tests/client/test_sms_notification_routes.py',
    'tests/client/test_sms_notification_route_full_app.py',
    'tests/test_desktop_tag_readback.py',
    'tests/test_native_window_reload_ci.py',
    'tests/client/test_preview_native_controls.py',
    'tests/client/test_office_close_returns_to_files.py',
    'tests/client/test_office_close_files_full_app.py',
    'tests/client/test_detached_sync_writer.py',
    'tests/client/test_sync_tick.py',
    'tests/test_native_window_reload_electron.py',
)

# Protocol and UI changes ship together. Include every Concord regression so new
# membership, invitation, call and recovery tests cannot miss the release gate.
TESTS = tuple(dict.fromkeys((*TESTS, *(
    str(path.relative_to(ROOT))
    for pattern in ('test_cord*.py', 'test_concord*.py')
    for path in sorted((ROOT / 'tests' / 'client').glob(pattern))
))))


def source_fingerprint(root):
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                              timeout=30).stdout
    digest = hashlib.sha256(git('rev-parse', '--verify', 'HEAD'))
    digest.update(git('diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--', *INPUTS))
    # git diff omits new, unstaged source files. Include them without following symlinks.
    for name in sorted(git('ls-files', '--others', '--exclude-standard', '-z', '--', *INPUTS).split(b'\0')):
        if not name:
            continue
        path = Path(root) / os.fsdecode(name)
        digest.update(b'\0' + name + b'\0')
        content = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def verify_receipt(path, root=ROOT):
    try:
        expected = json.loads(Path(path).read_text())['fingerprint']
        if expected != source_fingerprint(root):
            raise ValueError('tested source changed; rerun deployment checks')
        unstaged = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '-z',
                                   '--', *INPUTS], cwd=root, check=True, capture_output=True,
                                  timeout=30).stdout
        if unstaged:
            raise ValueError('stage new source files and rerun checks before deploying; git commit -a omits them')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('[regressions] ABORT: cannot verify tested source: ' + str(error))
        return 1
    print('[regressions] tested source is unchanged')
    return 0



# The 780-case gate passed locally in 210s, but GitHub completed 768 cases
# before the former 360s suite deadline. Budget for the full cold-runner suite;
# individual browser/network tests retain their own shorter deadlines.
def _run_required_tests(command, root, env, log, timeout=600):
    # Share the suite runner's owned process-group cleanup and file capture. Pipes
    # can stay open in orphaned browsers after pytest exits or is interrupted.
    captured = runpy.run_path(str(Path(__file__).with_name('checkall.py')))['_captured']
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return captured(command, root, dict(env, PC_GATE_MANAGED_PROCESSES='1'), timeout, log)
    except KeyboardInterrupt:
        return 130, '[regressions] required tests interrupted\n'
    finally:
        signal.signal(signal.SIGTERM, previous)


# Per-test durations from previous full runs, used only to balance shards. A cache, not a record:
# losing it costs balance, never coverage.
DURATIONS = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'posterchanai' / 'test-durations.json'


HANG_DUMP_S = 600


def discover_test_files(root):
    tests = Path(root) / 'tests'
    return sorted(str(p.relative_to(root)) for p in tests.rglob('test_*.py')
                  if not any(part in ('data', 'node_modules', '__pycache__') for part in p.parts))


def plan_shards(files, jobs, durations=None, root=ROOT):
    """Greedy balance: longest file first onto the least-loaded shard. Unknown files cost their size."""
    durations = durations or {}
    def cost(name):
        if name in durations:
            return float(durations[name])
        try:
            return (Path(root) / name).stat().st_size / 2000.0
        except OSError:
            return 1.0
    shards = [[] for _ in range(max(1, jobs))]
    load = [0.0] * len(shards)
    for name in sorted(files, key=cost, reverse=True):
        i = load.index(min(load))
        shards[i].append(name)
        load[i] += cost(name)
    return [sorted(shard) for shard in shards if shard]


def _file_of(case, root=ROOT):
    name = case.get('file') or ''
    if name:
        return name
    classname = case.get('classname') or ''
    parts = classname.split('.')
    for n in range(len(parts), 0, -1):
        candidate = '/'.join(parts[:n]) + '.py'
        if (Path(root) / candidate).exists():
            return candidate
    return classname


def _node_id(case, root=ROOT):
    """pytest node id from a junit testcase: tests/x/test_y.py::Class::test_z[param]."""
    path = _file_of(case, root)
    module = path[:-3].replace('/', '.') if path.endswith('.py') else ''
    classname = case.get('classname') or ''
    rest = classname[len(module):].lstrip('.') if module and classname.startswith(module) else ''
    return '::'.join(part for part in (path, *(rest.split('.') if rest else []), case.get('name') or '?') if part)


def _env_jobs():
    """`PC_GATE_JOBS` — shard count for a memory-constrained box. 0/unset means "decide by CPU"."""
    raw = (os.environ.get('PC_GATE_JOBS') or '').strip()
    if not raw.isdigit():
        return 0
    return max(0, min(64, int(raw)))


def run_full_suite(root, env, directory, jobs=None):
    """Every test file, in parallel shards. Returns (ok, message)."""
    files = discover_test_files(root)
    if not files:
        return False, 'no test files discovered'
    try:
        durations = json.loads(DURATIONS.read_text())
    except (OSError, ValueError):
        durations = {}
    if not jobs:
        # A GATE THAT CANNOT FIT IN MEMORY IS A GATE SOMEBODY SKIPS, which is how unlisted tests
        # failed against shipped code for days. `_default_jobs()` sizes itself by CPU, and each
        # shard imports the whole app -- so on a box whose RAM is already committed (a node running
        # Postgres with large shared buffers, measured here: six shards killed twice before the
        # first shard finished) the honest answer is fewer shards and a longer wait, never no gate.
        jobs = _env_jobs() or runpy.run_path(str(Path(__file__).with_name('checkall.py')))['_default_jobs']()
    shards = plan_shards(files, jobs, durations, root)
    checkall = runpy.run_path(str(Path(__file__).with_name('checkall.py')))
    captured = checkall['_captured']
    # One suite at a time per checkout (shared with ./test.sh): two would fight over the same CPU,
    # ports and browser profiles and report each other's timeouts as failures.
    # A gate started FROM a gate shard (this file's own tests) runs under the lock its parent holds.
    if os.environ.get('PC_GATE_MANAGED_PROCESSES') == '1':
        return _run_shards(root, env, directory, files, shards, durations, captured)
    try:
        with checkall['_runner_lock']():
            return _run_shards(root, env, directory, files, shards, durations, captured)
    except checkall['RunnerBusy'] as busy:
        return False, str(busy)


def _run_shards(root, env, directory, files, shards, durations, captured):
    print(f'[regressions] full suite: {len(files)} files in {len(shards)} parallel shards')
    started = time.monotonic()
    # The required pass disables plugin autoload for a minimal environment; the full suite needs the
    # plugins the repo's tests use (anyio runs every `@pytest.mark.anyio` test — without it each one
    # fails with "async def functions are not natively supported").
    env = {k: v for k, v in env.items() if k != 'PYTEST_DISABLE_PLUGIN_AUTOLOAD'}
    def run(index):
        report = Path(directory) / f'full-{index}.xml'
        # A hung test used to surface only as "shard did not complete (exit 124)" an hour later, naming
        # nothing. faulthandler (built into pytest) dumps every thread's traceback — test name included —
        # once one test runs past HANG_DUMP_S, into the shard log that the failure message tails.
        command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-o', 'addopts=',
                   '-o', f'faulthandler_timeout={HANG_DUMP_S}',
                   '--junitxml=' + str(report), *shards[index]]
        code, output = captured(command, root, dict(env, PC_GATE_MANAGED_PROCESSES='1'), 3600,
                                Path(directory) / f'full-{index}.log')
        return index, code, output, report
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(shards)) as pool:
        for index, code, output, report in pool.map(run, range(len(shards))):
            results.append((index, code, output, report))
    bad, cases, times = [], 0, {}
    for index, code, output, report in results:
        try:
            parsed = list(ET.parse(report).getroot().iter('testcase'))
        except (OSError, ET.ParseError):
            parsed = []
        if not parsed or code not in (0, 1):
            lines = output.strip().splitlines()
            dump = [i for i, l in enumerate(lines) if 'Timeout (' in l or 'most recent call first' in l]
            # Lead with the hang dump when there is one: it names the test; the last lines only show dots.
            tail = '\n'.join((lines[dump[0]:dump[0] + 40] if dump else []) + lines[-15:])
            bad.append(f'shard {index} did not complete (pytest exit {code}):\n{tail}')
            continue
        cases += len(parsed)
        for case in parsed:
            name = _file_of(case, root)
            times[name] = times.get(name, 0.0) + float(case.get('time') or 0)
            if case.find('failure') is not None or case.find('error') is not None:
                bad.append(_node_id(case, root))
        if code == 1 and not any(c.find('failure') is not None or c.find('error') is not None for c in parsed):
            bad.append(f'shard {index} exited 1 with no failing case recorded')
    if times:
        try:
            DURATIONS.parent.mkdir(parents=True, exist_ok=True)
            DURATIONS.write_text(json.dumps({**durations, **times}, sort_keys=True))
        except OSError:
            pass
    # A test that fails with six browsers competing for the CPU and passes on its own is timing-
    # sensitive, not broken — measured: the tablet launcher, repost-undo and remote-desktop browser
    # tests. Re-run the FAILING TESTS ONCE, SERIALLY; only what still fails blocks, and what passed is
    # printed so it stays visible. A shard that did not complete is never re-run away.
    flaky = []
    retry = [b for b in bad if '::' in b and not b.startswith('shard ')]
    if retry and len(retry) == len(bad):
        report = Path(directory) / 'full-retry.xml'
        code, output = captured([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-o', 'addopts=',
                                  '--junitxml=' + str(report), *retry], root,
                                 dict(env, PC_GATE_MANAGED_PROCESSES='1'), 1800, Path(directory) / 'full-retry.log')
        try:
            again = list(ET.parse(report).getroot().iter('testcase'))
        except (OSError, ET.ParseError):
            again = []
        if again and code in (0, 1):
            still = {_node_id(c, root) for c in again
                     if c.find('failure') is not None or c.find('error') is not None}
            ran = {_node_id(c, root) for c in again}
            flaky = [b for b in retry if b in ran and b not in still]
            bad = [b for b in retry if b not in flaky]
    elapsed = time.monotonic() - started
    if flaky:
        print('[regressions] FLAKY UNDER LOAD (failed in parallel, passed alone): ' + ', '.join(flaky))
    if bad:
        return False, f'{len(bad)} failing in the full suite ({elapsed:.0f}s):\n  ' + '\n  '.join(bad[:60])
    return True, f'{cases} cases across the full suite in {elapsed:.0f}s'


def run_gate(root=ROOT, receipt=None, full=False, jobs=0):
    try:
        before = source_fingerprint(root)
    except (OSError, subprocess.SubprocessError) as error:
        print('[regressions] ABORT: cannot identify source under test: ' + str(error))
        return 1
    # EVERY TEMP FILE THE RUN MAKES GOES IN ONE DIRECTORY THAT GOES WITH IT. /tmp is RAM on the
    # nodes this gates; a gate run is hundreds of Chromes and each leaves files behind in TMPDIR
    # (8 GB had piled up when a deploy was killed for low memory). See scripts/private_tmp.py.
    private_tmp = runpy.run_path(str(Path(__file__).with_name('private_tmp.py')))
    with tempfile.TemporaryDirectory(prefix='pc-deploy-regressions-') as directory, \
            private_tmp['scoped']('pct-gate-') as scratch:
        report = Path(directory) / 'results.xml'
        env = private_tmp['child_env'](
            dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PC_REQUIRE_NATIVE_IPC_TEST='1'), scratch)
        # Developer filters and mutation-test overrides must not change what a release tests.
        for name in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PC_SYNC_TEST_SOURCE',
                     'PC_OFFICE_TEST_SOURCE', 'PC_OFFLINE_APP_ROOT', 'PC_NATIVE_MAIN_SOURCE',
                     'PC_MMS_SOURCE_ROOT', 'PC_SMS_TEST_SOURCE'):
            env.pop(name, None)
        command = [sys.executable, '-m', 'pytest', '--noconftest', '-o', 'addopts=',
                   '-q', '-ra', '--durations=20', '--junitxml=' + str(report), *TESTS]
        try:
            code, output = _run_required_tests(command, root, env, Path(directory) / 'pytest.log')
        except (OSError, subprocess.TimeoutExpired) as error:
            print('[regressions] ABORT: required tests could not finish: ' + str(error))
            return 1
        print(output, end='')
        if code:
            print('[regressions] ABORT: pytest exited ' + str(code))
            return 1
        try:
            cases = list(ET.parse(report).getroot().iter('testcase'))
        except (OSError, ET.ParseError) as error:
            print('[regressions] ABORT: missing or invalid test results: ' + str(error))
            return 1
        if not cases:
            print('[regressions] ABORT: no test cases ran')
            return 1
        incomplete = [case for case in cases if any(case.find(tag) is not None
                                                   for tag in ('skipped', 'failure', 'error'))]
        if incomplete:
            names = ', '.join(case.get('name', '?') for case in incomplete)
            print('[regressions] ABORT: required coverage failed or was skipped: ' + names)
            return 1
        try:
            if before != source_fingerprint(root):
                raise ValueError('source changed while tests were running; rerun deployment checks')
            if receipt:
                Path(receipt).write_text(json.dumps({'fingerprint': before}))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print('[regressions] ABORT: ' + str(error))
            return 1
        print('[regressions] PASS: ' + str(len(cases)) + ' required cases, none skipped')
        if full:
            ok, message = run_full_suite(root, env, directory, jobs=jobs)
            if not ok:
                if receipt:
                    Path(receipt).unlink(missing_ok=True)   # the required pass wrote it; this run failed
                print('[regressions] ABORT: ' + message)
                return 1
            try:
                if before != source_fingerprint(root):
                    raise ValueError('source changed while the full suite was running; rerun deployment checks')
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                if receipt:
                    Path(receipt).unlink(missing_ok=True)
                print('[regressions] ABORT: ' + str(error))
                return 1
            print('[regressions] PASS: ' + message)
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--receipt', help='write a successful source fingerprint for this deploy')
    action.add_argument('--verify', help='check a receipt immediately before committing')
    parser.add_argument('--full', action='store_true',
                        help='also run every discovered test under tests/ in parallel shards (sync.sh does)')
    parser.add_argument('--jobs', type=int, default=0,
                        help='parallel shards for --full; fewer fits a box whose RAM is committed '
                             '(also PC_GATE_JOBS). 0 decides by CPU.')
    args = parser.parse_args()
    raise SystemExit(verify_receipt(args.verify) if args.verify
                     else run_gate(receipt=args.receipt, full=args.full, jobs=args.jobs))
