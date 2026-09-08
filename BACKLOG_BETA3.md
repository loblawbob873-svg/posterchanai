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

## After the LiveUSB ships — NVIDIA Quadro P1000 support

Requested 2026-09-08. The P1000 is Pascal (GP107). Do this only once the current ISO
has shipped and been confirmed on real hardware.

IT IS MOSTLY JUST `emerge nvidia-drivers` ON THE BUILD HOST. A kernel module is built
against the KERNEL, not against the hardware, so the laptop having no NVIDIA card does
not matter: the module is built for `6.18.43-gentoo-dist-bin`, mksquashfs packs
/lib/modules into the image, it sits unused on AMD machines and loads on the P1000. The
live initramfs is already built `--no-hostonly --conf /dev/null` (liveCD's dracut call),
so it inherits none of the build host's policy and is not hostonly-trimmed. The GPU
driver loads after switch_root from the squashfs, so it need not be in the initramfs at
all unless we want early KMS.

- [ ] Pin `x11-drivers/nvidia-drivers` to the **580** branch and VERIFY that pin against the
      Gentoo tree rather than from memory: 580 is understood to be the terminal branch for
      Maxwell/Pascal/Volta, with later branches dropping them, so an unpinned `-uDN @world`
      would eventually pull a driver that does not support this card.
- [ ] ACCEPT_LICENSE for NVIDIA's licence — no PosterChanOS install sets one today, and
      emerge refuses without it.
- [ ] `nvidia-drm.modeset=1` on the kernel command line. Wayfire is wlroots and without
      modeset the compositor gets no output; the symptom is a black screen, indistinguishable
      from every other black screen this project has produced. The live `append=` string is
      built in liveCD; the installed one in bootloader().
- [ ] Keep the module in step with kernel upgrades (dist-kernel subslot / @module-rebuild),
      or the first kernel update leaves the machine with no driver.
- [ ] NOUVEAU: check what the package already does before writing our own rule —
      nvidia-drivers ships its own modprobe policy, so we may need nothing. What exists in
      our tree is NOT a blacklist: `os/gentoo.sh:484` appends `omit_drivers+=" nouveau "` to
      /etc/dracut.conf, which keeps it out of the INITRAMFS only, and it sits inside the LUKS
      key-rotation function as a side effect of encrypted-boot setup. It also never reaches
      the live image, whose dracut runs with `--conf /dev/null`. Whatever we add must be
      conditional on the proprietary driver being installed and modesetting: blacklisting
      unconditionally leaves every NVIDIA machine with no driver at all, because nouveau is
      the only one in the image today and one ISO boots every machine.
- [ ] Watch the image size — nvidia-drivers is a few hundred MB on an image that is 3.2 GB.
- [ ] Test on the real P1000, not only in a VM. QEMU cannot reproduce a proprietary driver
      failing to modeset, exactly as it could not reproduce the 30-second network-online wait.

## Final follow-up requested 2026-09-08: private Monero zap announcement choice

- [ ] At the end of the Monero zap form, add an optional checkbox labelled "Do not post this zap".
  When selected, send the authorized payment without publishing a social post tagging the recipient.
  Preserve the existing announcement behavior when unchecked. Keep this task at the end of the
  backlog, after current stabilization and the earlier deferred work.
- [ ] Add behavior tests for both checkbox states, payment failure, ambiguous payment responses,
  and retries; changing this option must never send a second payment. Review before deployment.
