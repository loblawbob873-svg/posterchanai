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
import shlex
import signal
import subprocess
import sys
import tempfile
import re
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
    # Read on one device is read on all of them (pcai:seen), and a DM badges Messages, never the bell.
    'tests/client/test_seen_sync.py',
    'tests/client/test_read_state_follows_the_account_full_app.py',
    'tests/client/test_seen_sync_stores_only_its_own_doc_full_app.py',
    'tests/client/test_bell_clears_dms.py',
    'tests/client/test_dm_badges_messages_not_the_bell_full_app.py',
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
    'tests/client/test_notification_quote_box_fixed_indent_full_app.py',
    'tests/client/test_desktop_notification_history_full_app.py',
    'tests/client/test_desktop_queued_layout_edits_full_app.py',
    'tests/client/test_desktop_wallpaper_full_app.py',
    'tests/client/test_drive_file_found_on_another_server_full_app.py',
    'tests/client/test_nip71_video_posts_show_their_video_full_app.py',
    'tests/client/test_telegram_chat_catches_up_full_app.py',
    'tests/test_client_relay_eose_gate.py',
    'tests/client/test_private_doc_write_gates_hear_the_home_relay.py',
    'tests/client/test_private_docs_save_with_a_silent_relay_full_app.py',
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
    # 2026-10-10: the Postgres retirement and the day's regressions
    'tests/client/test_concord_accounts_kept_apart_full_app.py',
    # 2026-10-11: Concord reports from the APK (invite host, icon upload, menu jank)
    'tests/client/test_concord_invite_links_are_public_full_app.py',
    'tests/client/test_concord_icon_upload_says_so_full_app.py',
    'tests/client/test_concord_menus_stay_smooth_full_app.py',
    'tests/client/test_concord_member_right_click_full_app.py',
    'tests/client/test_dm_never_sends_nip44_to_nip04.py',
    'tests/client/test_mail_addresses_open_the_composer_full_app.py',
    'tests/client/test_mail_delete_is_instant_full_app.py',
    'tests/client/test_music_opens_in_front_full_app.py',
    # 2026-10-11: the desktop widget's Play/Shuffle PLAY (web desk + PosterChanOS), never open Music
    'tests/client/test_desktop_music_widget_plays_full_app.py',
    # 2026-10-11: the phone's top search bar slides away while reading (+ the synced pref round trip)
    'tests/client/test_search_bar_autohide_full_app.py',
    'tests/client/test_office_local_needs_no_membership_full_app.py',
    'tests/client/test_posted_track_carries_its_tags_full_app.py',
    'tests/client/test_a_relay_is_not_an_instance_full_app.py',
    'tests/test_doc_table.py',
    'tests/test_fedi_ledger_point_reads.py',
    'tests/test_list_all_docs_retries_a_page.py',
    'tests/test_mail_delete_batch.py',
    'tests/test_overlay_ebuild_versions.py',
    'tests/test_relay_says_which_part_of_an_event_is_invalid.py',
    'tests/test_script_tests_never_write_live_local_settings.py',
    'tests/test_settings_hydrate_retries_until_the_relay_answers.py',
    'tests/test_table_gate_sync_lookup_while_loading.py',
    'tests/test_table_load_retry_and_quiet_jobs.py',
    'tests/test_relay_langfilter.py',
    'tests/test_pcdb_bridge_scan_cost.py',
    'tests/test_posterchandb_prefix_index.py',
    'tests/test_posterchandb_deferred_words.py',
    'tests/test_relay_starts_before_bridge_scans.py',
    'tests/test_fedi_lookups_never_scan.py',
    'tests/test_loopback_relay_sockets_are_uncompressed.py',
    'tests/client/test_terminal_symbol_font.py',
    'tests/test_ptop.py',
    'tests/test_fresh_install_has_no_postgres.py',
    'tests/test_git_acceptance_asks_the_relay.py',
    'tests/test_posterchandb_words_off_lock.py',
    'tests/test_musicgeni_keeps_your_words.py',
    'tests/test_acestep_logs_no_lyrics.py',
    'tests/client/test_concord_code.py',
    'tests/client/test_concord_unasked_relays.py',
    'tests/client/test_concord_invite_list_create.py',
    'tests/client/test_save_never_opens_the_share_sheet.py',
    'tests/client/test_ai_download_native.py',
    'tests/test_drafts_nostr_only_accounts.py',
    'tests/test_tor_pool_least_busy.py',
    'tests/client/test_client_icons_are_icons_not_glyphs.py',
    'tests/client/test_concord_reads_before_writes_are_urgent.py',
    'tests/client/test_note_media_stays_where_it_was_written_full_app.py',
    'tests/client/test_generated_song_to_mp3_full_app.py',
    'tests/test_android_download_route.py',
    'tests/test_android_download_listener.py',
    'tests/test_android_media_save.py',
    'tests/client/test_native_download_handoff_full_app.py',
    'tests/client/test_ai_voice_label.py',
    'tests/test_bot_musicgeni.py',
    'tests/test_bot_runs_only_the_command_you_typed.py',
    'tests/test_every_bot_command.py',
    'tests/test_persona_never_breaks_character.py',
    'tests/test_generation_endpoints_require_auth.py',
    'tests/client/test_report_post_full_app.py',
    'tests/test_activitypub_flag.py',
    'tests/test_activitypub_app_doc_deletions.py',
    'tests/test_activitypub_delivery_never_stalls.py',
    'tests/test_server_stats_federation.py',
    'tests/client/test_server_stats_federation_full_app.py',
    'tests/test_admin_reports.py',
    'tests/client/test_report_from_notification_full_app.py',
    'tests/client/test_concord_report_private_full_app.py',
    # 2026-10-11: Bad Post of the Day -- the ⋯ vote (NIP-32 label) and the bot that announces it
    'tests/client/test_badpost_vote_full_app.py',
    'tests/test_badpost_bot.py',
    'tests/test_community_bots.py',
    'tests/test_voice_reference_size.py',
    'tests/client/test_voice_reference_once_full_app.py',
    # 2026-10-11: Concord banners, forum channels, voice presence on the channel list
    'tests/client/test_concord_banner_full_app.py',
    'tests/client/test_concord_forum_full_app.py',
    'tests/client/test_concord_voice_presence_full_app.py',
    'tests/client/test_concord_themes_full_app.py',
    'tests/client/test_concord_composer_fits_a_phone_full_app.py',
    'tests/client/test_concord_theme_tokens.py',
    # 2026-10-11: "No way to leave Community Settings" -- close/back/Escape/Android back, Save closes or says why
    'tests/client/test_concord_settings_has_a_way_out_full_app.py',
    # 2026-10-12: "concord shakes when changing room/community" (APK) -- the switch holds still
    'tests/client/test_concord_switch_is_steady_full_app.py',
    # 2026-10-12: "make our concord implementation [store everything on the relay] so each client don't have
    # to communicate with other concord relays" -- the node mirrors joined communities, clients read only it;
    # and Discover's "No public communities found" over a directory that held 70 invites
    'tests/test_concord_mirror.py',
    'tests/client/test_concord_reads_this_node_full_app.py',
    'tests/test_relay_tunables.py',
    'tests/test_admin_relay_sections.py',
    'tests/test_admin_relay_sections_browser.py',
    'tests/test_admin_settings_persist_today.py',
    'tests/test_admin_settings_persist_browser.py',
    # 2026-10-11: every admin list is ONE "Search or add" box (Enter / "+ Add", no duplicates, persists)
    'tests/test_admin_search_or_add_browser.py',
    'tests/client/test_bot_list_fields_full_app.py',
    # 2026-10-11: every synced client pref persists + hydrates; unsent changes survive a bad moment
    'tests/client/test_client_prefs_sync_full_app.py',
    # 2026-10-12: signed in, a notification / reply opens the WHOLE thread (held replies are expanded too)
    'tests/client/test_notification_opens_the_whole_thread_full_app.py',
    # 2026-10-12: PosterChanOS's local relay (osmode.py) -- the desktop's event cache instead of IndexedDB
    'tests/test_os_relay_contacts_only_the_instance.py',
    'tests/test_os_relay_private_docs_survive.py',
    'tests/test_os_relay_pins_match_the_client.py',
    'tests/test_os_relay_import_closure.py',
    'tests/test_os_relay_packaging.py',
    'tests/test_os_relay_write_volume.py',
    'tests/test_shell_waits_for_local_relay.py',
    'tests/client/test_store_local_relay_backend.py',
    'tests/client/test_local_relay_cache_full_app.py',
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
# 600s held until the list reached ~980 cases: green runs took ~570s on the Linux runner, and the
# next commits' tests pushed it over, killed at 97% with no test named (2026-10-01). The list only
# grows -- every new check joins it -- so the budget is sized for growth and overridable.
# 1200s held until 2026-10-11, when ~30 full-app checks joined in one day (reports, Concord, prefs,
# read state, admin persistence) and deploy 205's required stage was killed at 1200s with no failure: 2100s.
# .github/workflows/desktop.yml's step limit must stay ABOVE this, or GitHub kills the step before
# the gate can say what ran long; tests/test_deploy_regression_gate.py checks that.
REQUIRED_TIMEOUT_S = int(os.environ.get('PC_REQUIRED_TIMEOUT') or 2100)


def _run_required_tests(command, root, env, log, timeout=None):
    timeout = timeout or REQUIRED_TIMEOUT_S
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


def required_shards(entries, jobs, durations=None, root=ROOT):
    """The required list split for parallel runs, by FILE: a node id never leaves its file's group (a module's
    fixtures and order stay one process's), and each group keeps the list's own order."""
    files = list(dict.fromkeys(e.split('::')[0] for e in entries))
    shards = plan_shards(files, jobs, durations, root)
    where = {f: i for i, shard in enumerate(shards) for f in shard}
    groups = [[] for _ in shards]
    for e in entries:
        groups[where[e.split('::')[0]]].append(e)
    return [g for g in groups if g]


def _run_required_sharded(groups, base, root, env, directory, report, timeout=None):
    """THE REQUIRED STAGE RUNS IN PARALLEL GROUPS. Serially it outgrew every budget it was given (600s, 1200s,
    2100s): deploy 210 finished in 2006-2034s of 2100 and deploy 211 was killed at 2100s with ~930 of 2,246
    cases run and nothing named. Each group is its own pytest under the same flags, with a hang dump so a stuck
    test names itself; the junit results are merged into `report` for the checks that follow. A failure in the
    parallel run is re-run ONCE serially (the full stage's rule: failed under load, passed alone = flaky, shown);
    a skip is never re-run away, and a group that did not complete is never re-run away."""
    timeout = timeout or REQUIRED_TIMEOUT_S
    captured = runpy.run_path(str(Path(__file__).with_name('checkall.py')))['_captured']
    env = dict(env, PC_GATE_MANAGED_PROCESSES='1')
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    def run(index):
        part = Path(directory) / f'required-{index}.xml'
        command = [*base, '-o', f'faulthandler_timeout={HANG_DUMP_S}', '--junitxml=' + str(part), *groups[index]]
        code, output = captured(command, root, env, timeout, Path(directory) / f'required-{index}.log')
        return code, output, part
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(groups))) as pool:
            results = list(pool.map(run, range(len(groups))))
    except KeyboardInterrupt:
        return 130, '[regressions] required tests interrupted\n'
    finally:
        signal.signal(signal.SIGTERM, previous)
    out, cases, codes = [], [], []
    for index, (code, output, part) in enumerate(results):
        codes.append(code)
        if code not in (0, 1):
            lines = output.strip().splitlines()
            dump = [i for i, l in enumerate(lines) if 'Timeout (' in l or 'most recent call first' in l]
            out.append(f'[regressions] required group {index} did not complete (pytest exit {code}):\n'
                       + '\n'.join((lines[dump[0]:dump[0] + 40] if dump else []) + lines[-15:]) + '\n')
        else:
            out.append(output)
        try:
            cases.extend(ET.parse(part).getroot().iter('testcase'))
        except (OSError, ET.ParseError):
            if code in (0, 1):
                codes[-1] = 3      # a group that says it ran but left no readable results did not complete
    failed = [c for c in cases if c.find('failure') is not None or c.find('error') is not None]
    if failed and all(c in (0, 1) for c in codes):
        ids = list(dict.fromkeys(_node_id(c, root) for c in failed))
        part = Path(directory) / 'required-retry.xml'
        code, output = captured([*base, '--junitxml=' + str(part), *ids], root, env, timeout,
                                Path(directory) / 'required-retry.log')
        try:
            again = {_node_id(c, root): c for c in ET.parse(part).getroot().iter('testcase')}
        except (OSError, ET.ParseError):
            again = {}
        if code in (0, 1) and again:
            flaky = [i for i in ids if i in again and again[i].find('failure') is None
                     and again[i].find('error') is None and again[i].find('skipped') is None]
            if flaky:
                out.append('[regressions] FLAKY UNDER LOAD (failed in parallel, passed alone): '
                           + ', '.join(flaky) + '\n')
                cases = [again[_node_id(c, root)] if c in failed and _node_id(c, root) in flaky else c
                         for c in cases]
                if not [c for c in cases if c.find('failure') is not None or c.find('error') is not None]:
                    codes = [0 if c == 1 else c for c in codes]
    suite = ET.Element('testsuites')
    holder = ET.SubElement(suite, 'testsuite')
    holder.extend(cases)
    ET.ElementTree(suite).write(report)
    code = next((c for c in codes if c not in (0, 1)), None)
    if code is None:
        code = 1 if any(c == 1 for c in codes) else 0
    return code, ''.join(out)


# Per-test durations from previous full runs, used only to balance shards. A cache, not a record:
# losing it costs balance, never coverage.
DURATIONS = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'posterchanai' / 'test-durations.json'


HANG_DUMP_S = 600


# ---------------------------------------------------------------- remote shards: idle cores on another box
# "we need to deploy faster" (2026-10-22): the full suite is ~150 serial minutes and server1, which also serves,
# runs 4 shards at lowest priority. A second box's idle cores take part of it. MACHINE-LOCAL, never in the repo:
# ~/.config/posterchanai/gate-remote holds `user@host [jobs]` (or PC_GATE_REMOTE). That account must be a test-only
# user with no access to a live checkout -- tests write the settings of the checkout they run from, which is why
# test_git_proxy.py once took /git/ down on a live node. Every doubt resolves to running here: no config, an
# unreachable host, a sync that fails, or a shard that does not finish. A remote failure is re-run here (the retry-
# alone pass below), and what the remote SKIPPED is run here too -- so the remote can save time, never verdicts.
GATE_REMOTE_FILE = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'posterchanai' / 'gate-remote'
REMOTE_TREE = 'tree'
REMOTE_MAX_FAILURES = 10           # more on one remote shard = its environment; that shard runs here, whole                     # in the remote account's home; venv-unified there is its own venv
# Files that need what the remote venv deliberately lacks (torch/XPU, the ACE-Step and searxng checkouts).
_LOCAL_ONLY = re.compile(r"^\s*(?:import|from)\s+(?:torch|torchaudio|torchvision|acestep|searx)\b", re.M)
_SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=30']


def remote_target():
    """(user@host, jobs) when a remote is configured and answers, else None."""
    raw = (os.environ.get('PC_GATE_REMOTE') or '').strip()
    if not raw:
        try:
            raw = GATE_REMOTE_FILE.read_text().strip().splitlines()[0].strip()
        except (OSError, IndexError):
            return None
    if not raw or raw.lower() in ('off', 'none', '0'):
        return None
    parts = raw.split()
    host, jobs = parts[0], (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 6)
    try:
        ok = subprocess.run([*_SSH, host, f'test -x {REMOTE_TREE}/venv-unified/bin/python'],
                            capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        print(f'[regressions] remote shards: {host} did not answer -- running everything here')
        return None
    return host, max(1, min(16, jobs))


def remote_sync(root, host):
    """The working tree as it is NOW (tracked + untracked, not ignored) onto the remote; files gone here are
    removed there, so a deleted module cannot make an import pass remotely that fails here. True on success."""
    listed = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            cwd=str(root), capture_output=True)
    if listed.returncode != 0:
        return False
    names = [n for n in listed.stdout.decode('utf-8', 'surrogateescape').split('\0') if n]
    present = '\0'.join(n for n in names if (Path(root) / n).exists()).encode('utf-8', 'surrogateescape')
    try:
        sent = subprocess.run(['rsync', '-a', '-e', ' '.join(_SSH), '--from0', '--files-from=-', './',
                               f'{host}:{REMOTE_TREE}/'], cwd=str(root), input=present, capture_output=True,
                              timeout=600)
        if sent.returncode != 0:
            print('[regressions] remote shards: sync failed: ' + sent.stderr.decode(errors='replace')[-300:])
            return False
        prune = ("import os,sys\nkeep=set(sys.stdin.buffer.read().decode('utf-8','surrogateescape').split('\\0'))\n"
                 "os.chdir(os.path.expanduser('~/" + REMOTE_TREE + "'))\n"
                 "for d,dirs,files in os.walk('.'):\n"
                 " dirs[:]=[x for x in dirs if x not in ('venv-unified','__pycache__','.pytest_cache','node_modules')]\n"
                 " for f in files:\n"
                 "  n=os.path.join(d,f)[2:]\n"
                 "  if n not in keep: os.unlink(n)\n")
        pruned = subprocess.run([*_SSH, host, 'python3 -c ' + shlex.quote(prune)], input=present,
                                capture_output=True, timeout=300)
        return pruned.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def local_only(root, name):
    try:
        return bool(_LOCAL_ONLY.search((Path(root) / name).read_text(errors='replace')))
    except OSError:
        return False


# ---------------------------------------------------------------- server-only deploys skip browser tests
# A deploy that changes nothing a browser loads cannot be caught by a browser test, and those tests are about
# half the gate's time. "Server-only" is decided from the DIFF (what production runs = origin/master, plus
# whatever sync.sh is about to commit), and every doubt resolves to running them: an unreadable diff, an empty
# one, a path outside the list below, or PC_GATE_FULL_BROWSER=1. Routers, app/main.py, static/, templates/,
# desktop/, mobile/ and os/ are NOT on the list -- they shape what the client receives -- and neither are
# browser tests themselves, scripts/check_*, nor this gate's own files.
SERVER_ONLY = ('app/services/', 'app/models.py', 'app/database.py', 'app/schemas.py', 'app/worker.py',
               'botframework/', 'docs/', 'requirements', 'scripts/install/', 'install.sh', 'Dockerfile',
               'docker-entrypoint.sh', 'docker-compose.yml')
GATE_FILES = ('scripts/deploy_regression_gate.py', 'scripts/checkall.py', 'scripts/private_tmp.py', 'sync.sh')
# A test is a BROWSER test if it starts Chrome itself, runs a scripts/check_* browser check, or imports a test
# module that does (followed transitively). Read from the files, never a typed list, so a new one is classified
# by being written. "chromium" alone is NOT a marker: package names and prose use it.
_LAUNCH = re.compile(r"--remote-debugging-port|DevToolsActivePort|webSocketDebuggerUrl|/opt/google/chrome|"
                     r"google-chrome|--headless|scripts/check_\w+\.py|check_\w+\.py['\"]")
_IMPORT = re.compile(r"^\s*from\s+(tests(?:\.\w+)+)\s+import|^\s*import\s+(tests(?:\.\w+)+)", re.M)


def browser_test_files(root):
    """Every file under tests/ that drives a browser, directly or through a test module it imports."""
    root = Path(root)
    src = {}
    for p in (root / 'tests').rglob('*.py'):
        if any(part in ('data', 'node_modules', '__pycache__') for part in p.parts):
            continue
        try:
            src[str(p.relative_to(root))] = p.read_text(errors='replace')
        except OSError:
            continue
    browser = {f for f, s in src.items() if _LAUNCH.search(s)}
    deps = {f: {(m[0] or m[1]).replace('.', '/') + '.py' for m in _IMPORT.findall(s)} for f, s in src.items()}
    grew = True
    while grew:
        grew = False
        for f, d in deps.items():
            if f not in browser and d & browser:
                browser.add(f)
                grew = True
    return browser


def is_server_only_path(path):
    if path in GATE_FILES or path.startswith('scripts/check_'):
        return False
    if path.startswith('tests/'):
        return not path.startswith('tests/client/')
    if path.endswith('.md'):
        return True
    if path.startswith('scripts/'):
        return True
    return path.startswith(SERVER_ONLY)


def deploy_changes(root, base='origin/master'):
    """Paths this deploy changes (committed since `base` + not yet committed), or None when that cannot be told."""
    try:
        committed = subprocess.run(['git', 'diff', '--name-only', base + '...HEAD'], cwd=root, check=True,
                                   capture_output=True, text=True, timeout=60).stdout.split()
        pending = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root, check=True,
                                 capture_output=True, text=True, timeout=60).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    paths = set(committed)
    for line in pending:
        name = line[3:].split(' -> ')[-1].strip()
        if name:
            paths.add(name)
    return sorted(paths)


def browser_skip_decision(root):
    """(skip, why). Skip browser tests only for a deploy that provably changes no client-facing file."""
    if (os.environ.get('PC_GATE_FULL_BROWSER') or '').strip() not in ('', '0'):
        return False, 'PC_GATE_FULL_BROWSER is set'
    paths = deploy_changes(root)
    if paths is None:
        return False, 'could not read what this deploy changes'
    if not paths:
        return False, 'this deploy changes nothing (nothing to narrow)'
    client = [p for p in paths if not is_server_only_path(p)]
    if client:
        return False, '%d client-facing file(s) changed, e.g. %s' % (len(client), ', '.join(client[:3]))
    return True, 'server-only deploy (%d file(s): %s)' % (len(paths), ', '.join(paths[:4]) + (' …' if len(paths) > 4 else ''))


# PREFLIGHT: THE TESTS OF WHAT CHANGED, FIRST (2026-10-21, "improve testing so we can ship faster"). A deploy whose
# change breaks a test used to find out after the required pass AND the full suite -- ~20 minutes -- every time:
# three deploys in one afternoon each spent their 20 minutes to report one or two failures in a test that names
# the very file that changed. Preflight runs just those files (sharded, with the same retry-alone rule for a test
# that is only flaky under load) and stops the deploy in a minute or two when they fail; when they pass, the gate
# carries on exactly as before. It narrows nothing: every test still runs in the full suite.
_GENERIC_STEMS = frozenset({'app', 'client', 'main', 'index', '__init__', 'server', 'store', 'sw', 'utils', 'util',
                            'models', 'schemas', 'database', 'auth', 'config', 'common', 'base', 'test', 'run',
                            'manager', 'service', 'router', 'helpers', 'routes', 'events', 'session', 'settings'})
PREFLIGHT_MAX_SHARE = 0.35          # past a third of the suite it is not a shortcut any more
# Guards over the WHOLE client that never name the file they police, so name-matching cannot select them: a cache
# version every client change must bump, the cold-load byte budget. Run with any client change (deploy #6 found
# both only in the full suite, 50 minutes in).
_CLIENT_GUARDS = ('tests/test_a_client_change_bumps_the_cache.py', 'tests/test_client_boot_payload_budget.py')
_CLIENT_PREFIXES = ('static/', 'templates/')


def affected_test_files(root, paths, files=None):
    """Test files that mention a changed file (its name, or its module stem when the stem is specific enough),
    plus every changed test file. Sorted; [] when nothing is affected."""
    files = files if files is not None else discover_test_files(root)
    keys = set()
    changed_tests = set()
    for path in paths or []:
        name = Path(path).name
        stem = Path(path).stem
        if path in files:
            changed_tests.add(path)
            continue
        if path.startswith('tests/'):
            keys.add(name)                                  # a fixture/runtime a test loads by name
            continue
        keys.add(name)
        if stem not in _GENERIC_STEMS and len(stem) >= 6:     # 'term' is in "terminal" and "determine"
            keys.update({stem, stem.replace('-', '_'), stem.replace('_', '-')})
    hit = set(changed_tests)
    if any(str(p).startswith(_CLIENT_PREFIXES) for p in paths or []):
        hit.update(g for g in _CLIENT_GUARDS if g in files)
    if keys:
        import re as _re
        pat = _re.compile('|'.join('(?<![A-Za-z0-9_])' + _re.escape(k) + '(?![A-Za-z0-9_])' for k in sorted(keys)))
        for f in files:
            try:
                text = (Path(root) / f).read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            if pat.search(text):
                hit.add(f)
    return sorted(hit)


def run_preflight(root, env, directory, jobs=None, skip_browser=False):
    """(ran, ok, message, passed). Runs only when the selection is a real shortcut (not empty, under
    PREFLIGHT_MAX_SHARE). `passed` is the files it ran green: the full suite then leaves them out -- they just passed
    against this exact tree, and running them twice cost deploy #17 fourteen of its 75 minutes (244 files)."""
    paths = deploy_changes(root)
    if not paths:
        return False, True, 'preflight: nothing changed to select by', set()
    files = discover_test_files(root)
    sel = set(affected_test_files(root, paths, files))
    if not sel:
        return False, True, 'preflight: no test names a changed file', set()
    if len(sel) > PREFLIGHT_MAX_SHARE * len(files):
        return False, True, 'preflight: %d of %d test files affected -- the full gate is the shortcut' % (len(sel), len(files)), set()
    print(f'[regressions] preflight: {len(sel)} test file(s) that name what changed, first')
    ok, message = run_full_suite(root, env, directory, jobs=jobs, skip_browser=skip_browser, select=lambda f: f in sel)
    return True, ok, message, (set(sel) if ok else set())


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


def run_full_suite(root, env, directory, jobs=None, skip_browser=False, select=None):
    """Every test file, in parallel shards (browser tests left out of a server-only deploy). Returns (ok, message).
    `select` narrows the files (./test.sh runs tests/ and tests/client/ as two verdicts)."""
    files = discover_test_files(root)
    if select is not None:
        files = [f for f in files if select(f)]
    skipped = 0
    if skip_browser:
        browser = browser_test_files(root)
        kept = [f for f in files if f not in browser]
        skipped = len(files) - len(kept)
        files = kept
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
    remote = remote_target() if os.environ.get('PC_GATE_MANAGED_PROCESSES') != '1' else None
    if remote and not remote_sync(root, remote[0]):
        remote = None
    if remote:
        here_only = [f for f in files if local_only(root, f)]
        planned = plan_shards([f for f in files if f not in here_only], jobs + remote[1], durations, root)
        far, near = planned[:remote[1]], planned[remote[1]:] or [[]]
        for i, name in enumerate(here_only):
            near[i % len(near)].append(name)
        shards = [sorted(s) for s in near if s] + [sorted(s) for s in far]
        remote = (remote[0], len(shards) - len(far))      # (host, index of the first remote shard)
        print(f'[regressions] remote shards: {len(far)} on {remote[0]}, {len(shards) - len(far)} here')
    else:
        shards = plan_shards(files, jobs, durations, root)
    checkall = runpy.run_path(str(Path(__file__).with_name('checkall.py')))
    captured = checkall['_captured']
    # One suite at a time per checkout (shared with ./test.sh): two would fight over the same CPU,
    # ports and browser profiles and report each other's timeouts as failures.
    # A gate started FROM a gate shard (this file's own tests) runs under the lock its parent holds.
    note = (' (%d browser test file(s) skipped: server-only deploy)' % skipped) if skipped else ''
    if os.environ.get('PC_GATE_MANAGED_PROCESSES') == '1':
        ok, message = _run_shards(root, env, directory, files, shards, durations, captured, remote)
        return ok, message + note
    try:
        with checkall['_runner_lock']():
            ok, message = _run_shards(root, env, directory, files, shards, durations, captured, remote)
            return ok, message + note
    except checkall['RunnerBusy'] as busy:
        return False, str(busy)


def _run_shards(root, env, directory, files, shards, durations, captured, remote=None):
    print(f'[regressions] full suite: {len(files)} files in {len(shards)} parallel shards')
    started = time.monotonic()
    # The required pass disables plugin autoload for a minimal environment; the full suite needs the
    # plugins the repo's tests use (anyio runs every `@pytest.mark.anyio` test — without it each one
    # fails with "async def functions are not natively supported").
    env = {k: v for k, v in env.items() if k != 'PYTEST_DISABLE_PLUGIN_AUTOLOAD'}
    run_id = Path(directory).name

    def run_remote(index):
        host = remote[0]
        report = Path(directory) / f'full-{index}.xml'
        far = f'gate/{run_id}/full-{index}.xml'
        inner = ('mkdir -p gate/' + shlex.quote(run_id) + ' && cd ' + REMOTE_TREE + ' && PATH=$HOME/.local/bin:$PATH '
                 'PC_GATE_MANAGED_PROCESSES=1 exec nice -n 19 ionice -c 3 timeout 3600 venv-unified/bin/python -m pytest '
                 f'-q -p no:cacheprovider -o addopts= -o faulthandler_timeout={HANG_DUMP_S} --junitxml=$HOME/{far} '
                 + ' '.join(shlex.quote(f) for f in shards[index]))
        code, output = captured([*_SSH, host, inner], root, env, 3700, Path(directory) / f'full-{index}.log')
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', f'{host}:{far}', str(report)], capture_output=True, timeout=120)
        return index, code, output, report

    def run(index):
        if remote and index >= remote[1]:
            return run_remote(index)
        return run_here(index)

    def run_here(index):
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
    if remote:
        # What did not come back from the remote runs HERE: a shard that did not finish (its files, whole), and every
        # case the remote skipped (a skip there may be a tool or file it lacks -- here is the box that deploys).
        redo, again_skipped = [], []
        for i, (index, code, output, report) in enumerate(results):
            if index < remote[1]:
                continue
            try:
                parsed = list(ET.parse(report).getroot().iter('testcase'))
            except (OSError, ET.ParseError):
                parsed = []
            failed = sum(1 for c in parsed if c.find('failure') is not None or c.find('error') is not None)
            if not parsed or code not in (0, 1) or failed > REMOTE_MAX_FAILURES:
                # Many failures on one remote shard is the remote's environment, not the code: re-running them one at
                # a time (the retry pass) could take hours, so the whole shard runs here, in parallel, instead.
                why = f'did not finish (exit {code})' if not parsed or code not in (0, 1) else f'had {failed} failures'
                print(f'[regressions] remote shard {index} {why} -- its files run here')
                redo.extend(shards[index])
                results[i] = None
            else:
                again_skipped += [_node_id(c, root) for c in parsed if c.find('skipped') is not None]
        results = [r for r in results if r is not None]
        if again_skipped:
            print(f'[regressions] remote skipped {len(again_skipped)} case(s) -- running them here')
        extra = plan_shards(redo, max(1, remote[1]), durations, root) if redo else []
        if again_skipped:
            extra.append(again_skipped)
        if extra:
            first = len(shards)
            shards.extend(extra)
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(extra)) as pool:
                results.extend(pool.map(run_here, range(first, len(shards))))
        try:
            subprocess.run([*_SSH, remote[0], 'rm -rf gate/' + shlex.quote(run_id)], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass
    bad, cases, times, why = [], 0, {}, {}
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
                # The first line of the failure, kept for the flaky report: a test that passes when
                # re-run alone leaves this parallel failure as the ONLY record of why it failed.
                fail = case.find('failure') if case.find('failure') is not None else case.find('error')
                why[bad[-1]] = (str(fail.get('message') or fail.text or '').strip().splitlines() or [''])[0][:240]
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
        for name in flaky:
            if why.get(name):
                print(f'[regressions]   {name}: {why[name]}')
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
        preflight_passed = set()
        skip_browser, why = browser_skip_decision(root) if full else (False, '')
        if full:
            print('[regressions] browser tests: ' + ('SKIPPED — ' if skip_browser else 'run — ') + why)
            if os.environ.get('PC_GATE_NO_PREFLIGHT', '').strip() in ('', '0'):
                ran, ok, message, preflight_passed = run_preflight(root, env, directory, jobs=jobs, skip_browser=skip_browser)
                print('[regressions] ' + message)
                if ran and not ok:
                    print('[regressions] ABORT: the tests of what changed fail -- stopped before the full run')
                    return 1
        required = TESTS
        if skip_browser:
            browser = browser_test_files(root)
            required = tuple(t for t in TESTS if t.split('::')[0] not in browser)
        base = [sys.executable, '-m', 'pytest', '--noconftest', '-o', 'addopts=', '-q', '-ra', '--durations=20']
        try:
            durations = json.loads(DURATIONS.read_text())
        except (OSError, ValueError):
            durations = {}
        shards = jobs or _env_jobs() or runpy.run_path(str(Path(__file__).with_name('checkall.py')))['_default_jobs']()
        groups = required_shards(list(required), shards, durations, root)
        print(f'[regressions] required: {len(required)} entries in {len(groups)} parallel group(s)')
        try:
            code, output = _run_required_sharded(groups, base, root, env, directory, report)
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
            # A file the required stage just ran WHOLE (no `::test` narrowing) is not run a second time: the
            # required stage is the stricter of the two (no skips allowed), so its pass already covers it. It was
            # 14 of a deploy's ~66 minutes (2,316 required cases, every one repeated in the full suite).
            whole = {t for t in required if '::' not in t}
            ok, message = run_full_suite(root, env, directory, jobs=jobs, skip_browser=skip_browser,
                                         select=lambda f: f not in whole and f not in preflight_passed)
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


SUITES = {
    'tests': lambda f: not f.startswith('tests/client/'),
    'tests/client': lambda f: f.startswith('tests/client/'),
}


def run_suite(name, root=ROOT, jobs=0):
    """One half of the full suite, sharded, for ./test.sh. Serially, tests/ took 36 min and tests/client
    passed test.sh's 45-min cap without finishing, so `./test.sh` could not come back green at all; and a
    serial run is one process, where a test that leaks state (a stub left in sys.modules) breaks tests in
    files it never touches. Same shards and same flaky re-run as the gate, so the two cannot disagree.
    Prints a pytest-shaped summary line, which is what test.sh's board quotes."""
    private_tmp = runpy.run_path(str(Path(__file__).with_name('private_tmp.py')))
    with tempfile.TemporaryDirectory(prefix='pc-suite-') as directory, private_tmp['scoped']('pct-suite-') as scratch:
        env = private_tmp['child_env'](dict(os.environ), scratch)
        for drop in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
            env.pop(drop, None)
        ok, message = run_full_suite(root, env, directory, jobs=jobs, select=SUITES[name])
    print('[regressions] ' + message)
    if ok:
        print('%s passed' % message.split()[0])
        return 0
    print('%s failed' % message.split()[0])
    return 1


def run_changed(root=ROOT, jobs=0):
    """`--changed`: the preflight on its own -- the tests that name what changed, sharded, retry-alone for flakes. The
    quick answer before ./sync.sh (which runs it again first, then everything)."""
    private_tmp = runpy.run_path(str(Path(__file__).with_name('private_tmp.py')))
    with tempfile.TemporaryDirectory(prefix='pc-changed-') as directory, private_tmp['scoped']('pct-changed-') as scratch:
        env = private_tmp['child_env'](dict(os.environ), scratch)
        for drop in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
            env.pop(drop, None)
        paths = deploy_changes(root)
        sel = affected_test_files(root, paths or [])
        if not sel:
            print('[regressions] --changed: no test names a changed file')
            return 0
        print(f'[regressions] --changed: {len(sel)} test file(s)')
        ok, message = run_full_suite(root, env, directory, jobs=jobs, select=lambda f: f in set(sel))
    print('[regressions] ' + message)
    return 0 if ok else 1


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
    parser.add_argument('--changed', action='store_true',
                        help='run only the tests that name what changed (since origin/master + uncommitted), sharded: '
                             'the preflight on its own, a quick check before ./sync.sh; no receipt')
    parser.add_argument('--suite', choices=sorted(SUITES),
                        help='run only this half of the full suite, sharded (./test.sh); no required list, no receipt')
    args = parser.parse_args()
    if args.suite:
        raise SystemExit(run_suite(args.suite, jobs=args.jobs))
    if args.changed:
        raise SystemExit(run_changed(jobs=args.jobs))
    raise SystemExit(verify_receipt(args.verify) if args.verify
                     else run_gate(receipt=args.receipt, full=args.full, jobs=args.jobs))
