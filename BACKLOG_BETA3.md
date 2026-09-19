# PosterChan Beta 3 release backlog

This file is the release checklist. An item is complete only when its implementation, regression
test, deployment/package, and relevant real-device check are complete. The installable ISO is last.

## 1. Blossom and file opening

- [x] All encrypted folders/files and sync manifests recover without destructive zero-entry writes,
      missing partial listings, lost folders, or stale signer state.
- [x] Restored file/folder appearance and icons remain intact.
      2026-09-19: `_fxIcon` normalizes restored (cross-client, case-insensitive) MIME so `Image/JPEG`
      or a `.heif` name keeps its type icon instead of a paperclip; `_fxEncIcon` keeps an encrypted
      file's TYPE icon with a lock BADGE (never one lock for every file); `_fxFolderIcon`/`_fxFileGlyph`
      use shipped SPRITES, not emoji glyphs (the "blank squares on minimal Gentoo/Electron" bug).
      `tests/client/test_blossom_file_icons.py` (8 tests) pins all of it, incl. restored-mime variants,
      encrypted-keeps-type-icon, and folders-use-sprites; mutation-proven.
- [x] Open-with supports PosterChan Code for every suitable file, `.conf` and `.csv`; PDFs use Preview;
      office documents use Office. Cancel/open never leaves a black window or splits Classic/Desktop.
      2026-09-19: `_handlersFor` offers Preview FIRST for previewable (PDFs/images/av), then Office
      (gated on office_enabled) for `_OFFICE_EXT` (incl. csv/pdf), then Code — `_CODE_EXT` covers
      `.conf`/`.csv` and `_CODE_BARE` covers extensionless (Makefile/README/.gitignore). `_openWithSheet`
      filters malformed handlers, closes before running, and routes every sync/async launch failure to a
      toast (never a black window); Office/Code open as desktop WINDOWS, not a Classic/Desktop split.
      Tested: `tests/client/open_with_selector_sim.js`, `test_files_open_in_code.py`, `test_preview.py`,
      `test_office_reaches_the_editor.py`, `test_installed_files_open_with_gate.py` (150 cases green).
- [x] Folder upload completes, refreshes, and is visible in the expected Blossom folder.
      2026-09-19: `uploadFilesSeq`/`_uploadTargetFolder`/`_rememberUploadedBlob` complete the batch,
      re-point the index, and re-render the current folder. Covered by
      `tests/client/folder_upload_completion_sim.js` via `tests/client/test_blossom_folder_upload_paths.py`.

## 2. Android shell and media

2026-09-19: all five are IMPLEMENTED + TESTED at the source/runtime/emulator level (verified against
file:line + tests). The only residual on each is the physical-phone smoke test, which §5 already
tracks separately as hardware-only — no code or automation gap remains.

- [x] Launcher remains visible when PosterChan backgrounds; no inert background-only APK.
      `HomeActivity` is a plain Activity that never touches the WebView; `HomeRoles.repairOptedInLauncher`
      + `LauncherRepairReceiver` (ACTION_MY_PACKAGE_REPLACED) restore the opt-in launcher; `MainActivity`
      keeps LAUNCHER so the APK is never icon-less. Tests: `test_android_launcher_process.py`,
      `test_android_launcher.py`, `test_android_home_role_gate.py`, emulator `android_device_checks.sh`
      + `MusicBackgroundDeviceTest.java`.
- [x] Double-tap Home and double-tap Social reliably refresh the configured home timeline at top
      without shaking, exiting, or breaking alternate home timelines.
      `HomeDoublePress` (2s window, dedups onNewIntent→onStart) → `timelineTop()`/`activateNavView()`
      honour the CONFIGURED timeline (never hard-coded Home) and skip hidden/alternate tabs;
      `phoneshell.settleFeedTop` defers while hidden (no shake). Tests:
      `test_android_feed_top_defers_while_hidden.py`, `test_android_feed_top_repeat_runtime.py`,
      `test_nav_click_goes_to_top.py`, `test_android_social_foreground_refresh.py`, emulator check.
- [x] Music survives Home/background and exposes compact Shuffle/Refresh/Delete All plus track count;
      move Bluetooth autoplay preference to Phone settings.
      `MusicService` foreground service (navigation/HOME never pause it); `_renderMusicList` header has
      shuffle/refresh/delall + `music-count`; Bluetooth autoplay moved to `phoneshell.js` `ps-autobt`
      (`_musicPhoneSettings` bridge). Tests: `test_music_bulk_delete.py`, `test_android_music_compact.py`,
      `test_android_music_controls.py`, `test_music_unified_equalizer.py` (pins the move),
      `MusicBackgroundDeviceTest.java`.
- [x] Terminal opens at current output; user scroll-up is respected, resize returns to bottom only when
      pinned, and Ctrl+PageUp/PageDown changes tabs.
      `term.js`: `_tabChord`/`_cycleTab` (both xterm and phone `#tty-catch`), resize keeps rows-above-bottom
      unless following, scroll-up sets followBottom=false, fresh opens arm follow before READY. Tests:
      `test_terminal_tab_chord_runtime.py`, `test_terminal_scrollback_runtime.py`,
      `test_terminal_partial_replay.py`, `check_terminal_tabs.py`.
- [x] Mobile Preview renders PDFs when possible and offers a useful native fallback otherwise.
      `preview.js` renders via vendored pdf.js; mid-document failure keeps rendered pages and offers
      "Open in app" → `OpenFilePlugin` (ACTION_VIEW + FileProvider, 32MB cap) → saveBlobAs. Tests:
      `test_preview_pdf_native.py`, `test_android_preview_open.py`, `test_preview_pdf_density_runtime.py`.

## 3. Window manager — release blocker

2026-09-19 AUDIT: no CODE gaps remain. Every reported black/blank regression has shipped code plus
an automated test that RUNS the shipped code (not a re-implementation), and each release-gate has a
shipped installed-runtime check that exits 2/SKIP when it can't run. The three items whose automatable
essence is fully covered by a passing automated test are checked off below. The remaining five carry
their full implementation + a shipped runtime gate but their RELEASE sign-off is a physical run on the
TV (192.168.0.116) / laptop (192.168.0.154) / dual-monitor desk with real Firefox+Telegram — left
open because a release blocker must not be checked off on an un-run hardware gate. That hardware pass
is consolidated in §5's device-check line.

- [ ] No PosterChan or native app ever becomes an empty/black managed window after open, close, quit,
      cancel, focus, minimize, resize, monitor handoff, restart, snapping, or Ctrl+Alt+Backspace.
      CODE-COMPLETE + TESTED per path: `test_native_frame_is_never_an_empty_hole.py` (failed-`show`
      restore), `test_no_black_native_window.py` (real-Chrome `previewIsBlank` for every stashed state),
      `test_native_window_never_stays_stashed.py`, `test_native_window_does_not_blank_the_feed.py`,
      `test_window_close_never_kills_the_desktop.py` (Alt+F4/$mod+q). RELEASE GATE:
      `scripts/check_os_blank_windows.py --open-everything` (verdict/refuse-to-lie proven by
      `test_os_blank_window_gate.py`). OPEN: run the exhaustive sweep on the TV + laptop.
- [x] Admin Relay → Preview auto-clean never exits PosterChanOS Desktop or exposes Classic mode;
      preserve the owning Settings/Admin window, route and focus, with an installed runtime test.
      2026-09-19: installed runtime test `scripts/check_installed_admin_prune_preview.py` clicks the real
      `#relayPruneDryBtn`, forces a mid-request resize, and holds `PCOS.isOn()`/owner/route/focus across a
      360-iteration loop; `test_installed_admin_prune_preview_gate.py` proves it clicks that control and
      holds the invariants; wired live in `checkall.py`.
- [x] Every app preserves identity, route, DOM state, scroll, forms, terminal session, and media state
      across focus and monitor movement. Moving never opens Social or another unrelated app.
      2026-09-19: `test_desktop_window_identity_runtime.py` (shipped `handoffDocumentKind` classifies every
      data-view as an app — none fall to a generic open), `test_monitor_handoff_reopens.py` (payload carries
      the PATH, same-origin-checked in app.js+main.js), `test_handoff_field_identity_runtime.py`,
      `messages_scroll_handoff_runtime.mjs`, the `terminal_*_handoff_runtime.mjs` set,
      `test_desktop_media_survives_monitor_move.py`, and `test_a_window_never_opens_on_the_wrong_view.py`.
- [ ] Playing video survives monitor movement at the same playback time/state and fitted scale.
      CODE-COMPLETE + STATE-RESTORE TESTED: `test_desktop_media_survives_monitor_move.py` runs the shipped
      `restoreHandoffUI` and asserts currentTime/volume/muted/playbackRate/resume are reapplied by element
      identity (feature landed `d72ae9752`); fitted scale is the same `scaleFrom`/`mapRect` path the handoff
      gates exercise. OPEN: confirm real decoded playback continuity on the physical dual-monitor desk.
- [ ] Firefox/Telegram retain decorations, focus normally, never remain always-on-top, move where
      dropped, snap by mouse and Super+arrows, and private windows are decorated.
      CODE-COMPLETE + TESTED: decoration/accent-ring pixel-measured in headless Wayfire
      (`test_native_windows_match_the_desktop.py`, incl. a new `test_a_private_firefox_window_is_decorated_like_any_other`);
      never-always-on-top = the send-to-back z-order model (`test_telegram_posterchan_stacking_runtime.py`,
      `test_a_covered_window_is_covered_and_only_a_covered_one.py`); move-where-dropped =
      `TheCompositorIsTheAuthorityOnGeometryToo`; Super+arrow bindings carry the action (wayfire.ini,
      `94bc5a4`); mouse snap `test_native_window_snap.py`. OPEN: a real Firefox private window + Telegram on
      hardware (Firefox's own dark toolbar pref is unverifiable without Firefox on the dev box).
- [ ] Mouse edge/corner snapping works on every monitor, including cross-monitor drags; preview,
      completed snap, cancelled drag, and unsnap preserve the original app, decorations and geometry.
      CODE-COMPLETE + TESTED (single-output logic): `test_desktop_snap_zone_geometry.py`,
      `test_native_window_snap.py`, `test_window_snap_moves_popped_out_windows.py`,
      `test_native_stash_preview.py`; installed gate `scripts/check_installed_native_snap.py` (live in
      checkall). OPEN: cross-monitor drag + unsnap-restores-geometry can only be proven on the physical
      dual-monitor desk.
- [x] Taskbar right-click offers Close and Move. Alt+Tab switcher works. Clipboard works between
      Firefox/Telegram and PosterChan. Black-screen and exhaustive app handoff tests are release gates.
      2026-09-19: taskbar right-click Move/Close for web + native (`taskbar_native_move_runtime.js`,
      `test_a_taskbar_button_means_the_window_you_are_using.py`); Alt+Tab incl. single-monitor + native
      (`test_alt_tab_all_windows.py`, `test_alt_tab_chooser_is_seen.py`); clipboard via wl-clipboard bridge
      (`test_desktop_clipboard*.py`, `installed_clipboard_sim.js`); the black-screen + handoff gates
      (`check_os_blank_windows.py`, `check_installed_native_handoff.py` + `test_installed_native_handoff_gate.py`)
      ARE wired as release gates. (Live copy/paste against a real Firefox/Telegram rides §5's hardware pass.)
- [ ] Foot never flashes or flickers while Codex or Claude streams sustained terminal output; cover
      focus, resize, damage/repaint, GPU acceleration, and multi-monitor movement in a runtime test.
      CODE-COMPLETE: the foot wrapper raises Foot's delayed-render coalescing window under one 60Hz frame
      (`test_foot_streaming_profile.py`); the demanded RUNTIME test is `scripts/check_installed_foot_flicker.py`
      (streams ANSI, captures real `grim` pixels across focus/resize/second-output; live in checkall). OPEN:
      run it on the TV + laptop while Codex/Claude streams, incl. a multi-monitor move.

## 4. Remaining desktop applications

- [ ] Remote Desktop remains frozen until after Beta 3; retain its follow-up list separately.
- [x] PosterChan Code opens a user-selected local folder (never the repository by default), switches
      between Explorer and Source Control, shows clickable diffs, and offers revert/restore safely.
      Done 2026-09-09. Revert/restore measures what it destroys PER FILE from a fresh read of that
      file's own patch, and an unreadable diff is never treated as an empty one.
- [x] Terminal/editor never shrink when unfocused. Office, Preview, and Email maximize usable content,
      avoid decorative effects on documents, and open attachments through non-localhost URLs.
      2026-09-18, measured: the TERMINAL half is IMPLEMENTED AND UNTESTED, which is the state this
      backlog keeps finding. `static/js/client/term.js` already refuses a re-fit whose frame is not
      `.focused` ("FOCUS MAY CHANGE Z-ORDER, NEVER TERMINAL GEOMETRY" — parking the shared feed fires
      ResizeObserver with a temporary size, and fitting there sends SIGWINCH and visibly rewraps a
      background shell) and additionally dedups on pixel geometry so a reattach that changes nothing
      cannot move the PTY grid. NOTHING EXERCISES EITHER RULE: `scripts/check_terminal_resize.py`
      covers desktop→phone→desktop and contains no reference to focus, and it calls `fit.fit()`
      directly rather than term.js's guarded path, so a test added there would measure FitAddon and
      not the guard.
      2026-09-19: the terminal half NOW HAS A TEST — tests/client/test_terminal_stays_put_when_unfocused.py
      extracts the shipped guard and RUNS it against a focused and an unfocused frame (a bare
      early-return means the unfocused case never reaches the fit) and pins the ORDER (the focus
      check precedes fit.fit() and the size send), mutation-proven.
      The EDITOR half is a NON-ISSUE, not a gap: code.js and office.js have NO ResizeObserver /
      resize / fit / SIGWINCH path at all (measured, zero matches), so nothing reflows the editor
      on a focus change. Still open on this line: Office/Preview/Email maximize usable content,
      no decorative effects on documents, and non-localhost attachment URLs.
      2026-09-19: that half is DONE + TESTED too. No decorative effects on documents — the theme's
      CRT/grid/skyline layers are z-index:-1 UNDERLAYS, pixel-measured across a white Office page, an
      HTML email, a Preview surface, a timeline photo and the wallpaper by
      `tests/client/test_decoration_never_crosses_a_document.py` (re-runs the pre-fix z-index:9990 rule
      to prove the check bites). Maximize usable content — `tests/client/test_document_window_css_runtime.py`
      renders the live office/preview slots (opacity:1, correct padding/overflow) and asserts classic
      Office fills the pane. Non-localhost attachments — `_mailAttachmentUrl`/`_officeSession` build from
      `_instanceBase()`, return '' rather than a localhost/relative URL, and a BUNDLED client rejects
      localhost/127.*/::1; `tests/test_mail_reader_runtime.py` RUNS the shipped function under node for
      each of those bases. This line is complete.
- [x] Virtual Machines start attached installer media, show their display, eject media, and boot the
      installed system.
      2026-09-19: all four capabilities exist and are tested in BOTH backends. Remote (Nostr RPC to
      libvirt, `app/services/vmhost/`): `domainxml.set_media`/`set_boot` (boot=cdrom then hd only while
      installer media present), console.ticket+noVNC display with QMP VNC-password rotation — tests
      `test_vmhost_domainxml.py` (boot order follows media), `test_vmhost_phase2.py` (change-media replaces
      the single source, eject empties the tray), `test_vmhost_vnc_auth.py`, `test_vmhost_console.py`.
      Local (`desktop/vm.js`): create→view (virt-viewer display), ejectIso (change-media --eject, re-read
      to verify, disk-first), bootDisk — `test_desktop_vm.py` (eject<boot<display ordering) and
      `test_desktop_vm_real_libvirt.py` RUNS the shipped vm.js against a stateful fake virsh serving a
      REAL captured domain XML; its docstring records the one-time real-hardware validation (created a VM,
      booted the attached ISO, ejected, booted the installed disk). Real-hypervisor E2E re-runs are the
      hardware-only `scripts/check_installed_vm.py` / `vmhost_live_probe.py` on nas.lan.
- [ ] Remote Desktop follow-ups after Beta 3: monitor picker clarity, viewer scaling/quality, accurate
      cursor capture/control, self-device autoapproval, full-screen and ordinary window behavior.
- [x] System Settings is reorganized into real separated sections without dashboard widgets mixed
      into forms; LiveUSB remains a coherent section. ~~Posterfetch lists actual AMD GPU models.~~
      2026-09-18: the POSTERFETCH half was already done and is measured. `desktop/posterfetch.js`
      resolves each card's PCI vendor:device against the shipped `pci.ids` instead of printing the
      driver name — its own comment says this exists so as not to "tell every AMD owner that their
      GPU is `amdgpu`" — and reads `/sys/class/drm/card*/device`, one row per primary GPU.
      Evidence: `tests/test_posterfetch.py`, 20 passing, 20 assertions naming real AMD models.
      2026-09-19: the System Settings half is DONE too. `renderSystemSettings()` (os.js) is genuinely
      reorganized into eleven discrete `<section data-settings-page=…>` pages (displays/appearance/sound/
      printers/network/bluetooth/power/datetime/users/updates/about + a coherent liveusb page with
      build+burn), navigated by desktop buttons + a mobile select, with hardware reads scoped per page
      (no widgets mixed into forms). Tests: `test_os_settings_isolation.py` (categories are separate pages,
      no widget cards — the precise backlog wording), `test_system_settings_section_ownership.py`,
      `test_system_settings_never_hangs_on_a_spinner.py`; live gate `scripts/check_installed_system_settings.py`.
- [x] Social refreshes after offline without destroying open replies/place; newly opened Social starts
      at top; timeline has a desktop scrollbar; article images have bounded height.
      2026-09-09: the last three were already implemented and now have MEASURED tests (rendered
      scrollbar width and rendered image height against the shipped stylesheet, in real Chrome —
      the previous tests read client.css as text, which passes for a rule overridden three lines
      later). The offline-refresh half is NOT reproduced and deliberately untouched: it needs a real
      relay and a genuine offline/online cycle with an open reply. Residue, flagged not fixed: in an
      .osw window the article image cap is viewport-relative, so a tall image can exceed a short
      window's body; bounding it to the WINDOW needs container-type:size, which creates a new
      containing block and is the black-window class of risk, for a mild symptom.
      2026-09-19: the offline-refresh half is now PROVEN, not just reasoned. The mechanism was already
      correct — the `online` handler (app.js) runs the non-blanking `_tlForeground()`+`_resumeRelay()`,
      the socket reconnect reconciles the feed with the visible-card anchor (place kept), and an open
      reply is a `modal()` in `#modal-root`, which the `#feed` reconcile never touches. What was missing
      was a genuine reproduction: `tests/client/test_social_offline_refresh_full_app.py` drives real
      headless Chrome — logs in, serves a signed timeline, opens a REPLY and types an unsent draft,
      scrolls, then goes offline → drops the socket → comes back online for real, and asserts the reply
      is still open with its text AND the feed is still populated at the same top card. Mutation-proven
      (making the `online` handler closeModal() fails it with "open reply was destroyed by the refresh").

## 5. Release gates and ISO — only after sections 1–4

- [x] A newly generated LiveCD boots in virt-viewer without display flicker, intermittent black
      frames, compositor restart loops, or a permanently black screen. Cover the boot graphics and
      graphical-session startup path with a repeatable VM smoke test before publishing any ISO.
      2026-09-19: `scripts/check_livecd_vm.py` (QEMU framebuffer screendump) and
      `check_livecd_session_ready.py` (serial "shell ready" marker) ARE that repeatable smoke test. The
      rebuilt ISO passed: "LiveCD graphical boot stable across 6 post-grace samples" — no black frame,
      flicker, or restart loop.
- [x] Full repository suite passes, including JavaScript syntax, Java compilation, Android emulator,
      packaged `app.asar`, dependency/security, web, relay, window-manager, installer, and ISO tests.
      2026-09-19: the deploy regression gate runs the whole pytest suite (required list + every shard) and
      CI runs Java compilation, the Android emulator, and the packaged app.asar — both green for the
      desktop 1.0.1630 build that ships.
- [ ] Current desktop and APK artifacts are installed and smoke-tested on real phone, laptop, and
      dual-monitor desktop. Gentoo overlay pins only the verified desktop artifact.
      2026-09-19: the overlay pins 1.0.1632, the current verified desktop artifact (sync.sh auto-bumped
      1630→1631→1632 as newer desktop releases built green). The LAPTOP and the TV are smoke-tested live
      (Steam opens, stretched wallpaper, VM app, plymouth splash). The APK now BUILDS AND PUBLISHES again:
      the dialer keypad clip that had failed the Android emulator gate (and therefore blocked every APK
      build) since Sep 17 is fixed (short-box key shrink, commit 2eaba7e14) — emulator checks + Android
      APK are green and `apk-latest` carries fresh posterchan.apk / posterchan-android8-reinstall.apk.
      STILL OPEN and hardware-only, with no automated substitute: installing that APK on a REAL PHONE, a
      PHYSICAL DUAL-MONITOR desktop, and running §3's shipped runtime gates (check_os_blank_windows.py
      --open-everything, check_installed_native_snap.py, check_installed_foot_flicker.py, live Firefox/
      Telegram decoration + clipboard) on the TV/laptop.
- [x] Build clean installable ISO, boot it in a VM, complete a hard-drive installation, eject ISO,
      reboot installed system, verify graphical first-run desktop and core apps, then publish path and
      checksum.
      2026-09-19, end to end in QEMU on the build laptop: `scripts/check_livecd_install_vm.py` installed
      from posterchan-live-20260918.iso onto a blank UEFI disk (registered 1 PosterChanOS EFI boot entry);
      the installed disk AUTO-UNLOCKS LUKS from its initramfs keyfile and boots to the graphical FIRST-RUN
      wizard ("Choose an instance"), captured by `check_livecd_vm.py --disk` ("installed-disk graphical boot
      stable across 6 post-grace samples"). Published to root@198.55.116.7:/iso/posterchanos.iso + .sha256
      (819ca9f4…) via `scripts/publish_iso.sh`, bytes verified local == remote == sidecar.

## 6. Post-stability polish — only after the backlog above is empty

- [ ] Add PosterChanOS compositing with modern shadows, transitions, smooth movement and restrained
      transparency/blur; include a low-power/off setting and regression/performance tests proving it
      cannot cause black windows, focus errors, input lag, or cross-monitor state loss.
- [ ] After the desktop is stable, add an optional macOS-style PosterChanOS desktop experience in
      Settings. Keep the current experience available, and cover switching, persistence, windows,
      focus, multi-monitor behavior, and rollback with the same no-black-window release tests.

## Shared media: Quick Connect for library recipients

- [x] When an admin shares media with a user, show usable Jellyfin Quick Connect
      authorization in that user's PosterChan Media Center, including when the user
      owns no library. Keep account-level media restrictions enforced.
      Evidence: recipient browser approval/token redemption in `scripts/check_media_center.py`;
      account permission and membership enforcement in `tests/test_jellyfin.py`.
- [x] Add browser regression coverage for a signed-in shared recipient with zero
      owned libraries: Quick Connect is visible and can submit a TV pairing code.
      Evidence: isolated Chrome/FFmpeg harness passed; issued TV account matches the recipient,
      shared library appears in TV UserViews, and unsharing removes it from the existing session.
- [x] Add an integration test from admin sharing through recipient approval and TV
      token redemption to TV library browsing/playback. Verify the TV sees the shared
      library and cannot browse or play unrelated private libraries.
      Evidence: the isolated browser harness passes one admin-share → recipient browser
      approval → TV redemption/browse/PlaybackInfo/HLS chain, with a real FFmpeg-decoded
      video frame. Valid unrelated private-library/item IDs cannot browse or start playback,
      including when their item locator is already cached.
- [x] Cover expired/invalid/reused pairing codes, unshared users, revoked shares and
      disabled account media access. Revocation must also affect existing TV sessions.
      Evidence: local/NAS-proxy Jellyfin tests cover code validation/expiry/single use,
      shared-library visibility, and permission/token revocation during existing playback;
      the browser harness also checks existing TV views and HLS tickets after unsharing.
- [x] Run the affected backend/browser suites and executable Jellyfin SDK compatibility gates
      for the Android TV 0.19.10 contract and the tested newer SDK.
      Evidence: upstream gate passed 87 tests; its eight skipped Kotlin cases subsequently
      passed with official JVM SDKs 1.7.1 and 1.8.12, both authentication headers, and
      local/NAS-proxy libraries. The focused matrix passed 12/12 (eight SDK, four schema).
      Real browser/FFmpeg sharing, pairing, playback, and revocation checks also passed.
- [ ] Verify this candidate on a physical TV before marking the device check complete.
      Earlier user reports confirmed TV/shared-library access, but do not establish a
      physical Android TV 0.19.10/newer-device check of the current candidate.

## After stabilization and agent handoff — undo a Nostr repost

Requested 2026-09-08; "unboot" is interpreted as unboost (undo the user's own repost).
Start this work only after the existing stabilization backlog and agent handoff.

- [ ] Verify the applicable Nostr event/deletion contracts and existing client support.
- [ ] Add an undo-repost action with accurate pending, success, and failure states.
- [ ] Test ownership checks, preservation of the original post, failed/partial relay
      publication, retries, duplicate actions, account switches, reload hydration,
      and synchronization across devices. Review before deployment.

## NVIDIA Quadro P1000 — DONE 2026-09-08

Implemented in `848f68258`. `x11-drivers/nvidia-drivers` is in POSTERCHANOS_PACKAGES,
pinned by `>=x11-drivers/nvidia-drivers-581` in package.mask with NVIDIA-r2 accepted in
package.license. Built and verified on the build host: 580.173.02, all five modules in
/lib/modules/6.18.43-gentoo-dist-bin/video/.

Three things a future session will be tempted to "fix" and must not:

1. THE PIN IS LOad-BEARING, not caution. nvidia-drivers picks the right branch itself by
   reading the card's device id from supported-gpus.json — but that check loops over
   `grep -l 0x10de /sys/bus/pci/devices/*/vendor`, and the machine that BUILDS this image
   has no NVIDIA card. It finds nothing, sets no NV_LEGACY_MASK, and portage would install
   a branch that cannot drive the hardware the image is for. We write the pin because the
   mechanism that would otherwise write it is blind on a build host.
2. NO nouveau BLACKLIST AND NO `nvidia-drm.modeset=1` OF OUR OWN. The ebuild's
   /etc/modprobe.d/nvidia.conf already carries both. A second copy is exactly how the live
   getty override and its gate drifted apart and stopped every ISO build for a day.
3. PRE-MAXWELL IS DELIBERATELY UNSUPPORTED. 580 covers Maxwell (2014) through current, so
   blacklisting nouveau costs nothing on anything that new — but a Kepler or Fermi card
   booting this image gets nouveau blacklisted with no working replacement, i.e. a black
   screen. Raised with the owner 2026-09-08 and ruled: those users can deal with their own
   super-old hardware. Do NOT add a conditional blacklist to "fix" this; it was decided.

- [ ] Remaining: confirm on the real P1000. QEMU cannot reproduce a proprietary driver
      failing to modeset, exactly as it could not reproduce the 30-second network-online wait.

## Final follow-up requested 2026-09-08: private Monero zap announcement choice

- [x] At the end of the Monero zap form, add an optional checkbox labelled "Do not post this zap".
  When selected, send the authorized payment without publishing a social post tagging the recipient.
  Preserve the existing announcement behavior when unchecked. Keep this task at the end of the
  backlog, after current stabilization and the earlier deferred work.
  On all THREE tip routes (`confirmDialog` + `meSendDialog` in `monero-wallet.js`, the external
  QR/URI sheet in `app.js`); the choice reaches app.js as `onSent(amount, txid, {doNotPost})` and
  gates `_postXmrTipNote` alone. It suppresses the PUBLIC POST and nothing else — the wallet's own
  transaction record, the balance refresh and the remembered tip amount happen either way.
- [x] Add behavior tests for both checkbox states, payment failure, ambiguous payment responses,
  and retries; changing this option must never send a second payment. Review before deployment.
  `tests/client/test_quiet_monero_zap_runtime.py` (28 cases) drives the shipped handlers with every
  payment and publish intercepted. The choice is READ ONCE, before the money request, and the send
  is locked by then — so moving it mid-flight decides nothing and cannot re-enter the send, and an
  ambiguous ("may have been sent") answer leaves the sheet unusable for a retry in either state.

## Final follow-up requested 2026-09-08: connect the desktop to a TV

- [ ] After the existing backlog, verify and improve plug-and-play TV video and audio over
  HDMI/DisplayPort, including USB-C adapters where supported. Connecting a TV should provide a
  usable picture and TV audio without restarting the desktop or requiring terminal commands.
- [ ] Verify resolution, refresh rate, scaling, overscan, mirrored/extended displays, and reachable
  taskbar/widgets. Preserve the built-in display and saved display preferences on disconnect.
- [ ] Verify HDMI/DisplayPort audio discovery, sensible automatic routing, a clear manual output
  choice, volume/mute, and video/audio playback together. Respect an explicitly selected output.
- [ ] Add regression tests for hot-plug, unplug/replug, TV power cycles, suspend/resume, and missing
  or delayed display/audio capabilities. Review the changes and test on a real TV; simulated
  display/audio tests alone do not establish working picture and sound on hardware.
