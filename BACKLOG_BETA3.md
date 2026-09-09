# PosterChan Beta 3 release backlog

This file is the release checklist. An item is complete only when its implementation, regression
test, deployment/package, and relevant real-device check are complete. The installable ISO is last.

## 1. Blossom and file opening

- [x] All encrypted folders/files and sync manifests recover without destructive zero-entry writes,
      missing partial listings, lost folders, or stale signer state.
- [ ] Restored file/folder appearance and icons remain intact.
- [ ] Open-with supports PosterChan Code for every suitable file, `.conf` and `.csv`; PDFs use Preview;
      office documents use Office. Cancel/open never leaves a black window or splits Classic/Desktop.
- [ ] Folder upload completes, refreshes, and is visible in the expected Blossom folder.

## 2. Android shell and media

- [ ] Launcher remains visible when PosterChan backgrounds; no inert background-only APK.
- [ ] Double-tap Home and double-tap Social reliably refresh the configured home timeline at top
      without shaking, exiting, or breaking alternate home timelines.
- [ ] Music survives Home/background and exposes compact Shuffle/Refresh/Delete All plus track count;
      move Bluetooth autoplay preference to Phone settings.
- [ ] Terminal opens at current output; user scroll-up is respected, resize returns to bottom only when
      pinned, and Ctrl+PageUp/PageDown changes tabs.
- [ ] Mobile Preview renders PDFs when possible and offers a useful native fallback otherwise.

## 3. Window manager — release blocker

- [ ] No PosterChan or native app ever becomes an empty/black managed window after open, close, quit,
      cancel, focus, minimize, resize, monitor handoff, restart, snapping, or Ctrl+Alt+Backspace.
- [ ] Admin Relay → Preview auto-clean never exits PosterChanOS Desktop or exposes Classic mode;
      preserve the owning Settings/Admin window, route and focus, with an installed runtime test.
- [ ] Every app preserves identity, route, DOM state, scroll, forms, terminal session, and media state
      across focus and monitor movement. Moving never opens Social or another unrelated app.
- [ ] Playing video survives monitor movement at the same playback time/state and fitted scale.
- [ ] Firefox/Telegram retain decorations, focus normally, never remain always-on-top, move where
      dropped, snap by mouse and Super+arrows, and private windows are decorated.
- [ ] Mouse edge/corner snapping works on every monitor, including cross-monitor drags; preview,
      completed snap, cancelled drag, and unsnap preserve the original app, decorations and geometry.
- [ ] Taskbar right-click offers Close and Move. Alt+Tab switcher works. Clipboard works between
      Firefox/Telegram and PosterChan. Black-screen and exhaustive app handoff tests are release gates.
- [ ] Foot never flashes or flickers while Codex or Claude streams sustained terminal output; cover
      focus, resize, damage/repaint, GPU acceleration, and multi-monitor movement in a runtime test.

## 4. Remaining desktop applications

- [ ] Remote Desktop remains frozen until after Beta 3; retain its follow-up list separately.
- [ ] PosterChan Code opens a user-selected local folder (never the repository by default), switches
      between Explorer and Source Control, shows clickable diffs, and offers revert/restore safely.
- [ ] Terminal/editor never shrink when unfocused. Office, Preview, and Email maximize usable content,
      avoid decorative effects on documents, and open attachments through non-localhost URLs.
- [ ] Virtual Machines start attached installer media, show their display, eject media, and boot the
      installed system.
- [ ] Remote Desktop follow-ups after Beta 3: monitor picker clarity, viewer scaling/quality, accurate
      cursor capture/control, self-device autoapproval, full-screen and ordinary window behavior.
- [ ] System Settings is reorganized into real separated sections without dashboard widgets mixed
      into forms; LiveUSB remains a coherent section. Posterfetch lists actual AMD GPU models.
- [ ] Social refreshes after offline without destroying open replies/place; newly opened Social starts
      at top; timeline has a desktop scrollbar; article images have bounded height.

## 5. Release gates and ISO — only after sections 1–4

- [ ] A newly generated LiveCD boots in virt-viewer without display flicker, intermittent black
      frames, compositor restart loops, or a permanently black screen. Cover the boot graphics and
      graphical-session startup path with a repeatable VM smoke test before publishing any ISO.
- [ ] Full repository suite passes, including JavaScript syntax, Java compilation, Android emulator,
      packaged `app.asar`, dependency/security, web, relay, window-manager, installer, and ISO tests.
- [ ] Current desktop and APK artifacts are installed and smoke-tested on real phone, laptop, and
      dual-monitor desktop. Gentoo overlay pins only the verified desktop artifact.
- [ ] Build clean installable ISO, boot it in a VM, complete a hard-drive installation, eject ISO,
      reboot installed system, verify graphical first-run desktop and core apps, then publish path and
      checksum.

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

- [ ] At the end of the Monero zap form, add an optional checkbox labelled "Do not post this zap".
  When selected, send the authorized payment without publishing a social post tagging the recipient.
  Preserve the existing announcement behavior when unchecked. Keep this task at the end of the
  backlog, after current stabilization and the earlier deferred work.
- [ ] Add behavior tests for both checkbox states, payment failure, ambiguous payment responses,
  and retries; changing this option must never send a second payment. Review before deployment.

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
