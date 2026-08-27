# PosterChan Beta 3 release backlog

This file is the release checklist. An item is complete only when its implementation, regression
test, deployment/package, and relevant real-device check are complete. The installable ISO is last.

## 1. SMS / MMS / Texts

- [ ] Full SMS/MMS history and media converge between Android, web UI, and PosterChanOS.
- [ ] Web/desktop can send MMS attachments through the phone; delivered messages are never reported
      failed merely because the carrier callback is ambiguous.
- [ ] Pending/failed sends reconcile, retry, cancel, and delete on every surface without ghosts or
      duplicates. Old queued MMS retries intentionally.
- [ ] Camera, device, and account-scoped Blossom attachment sources work on Android and web.
- [ ] Images/video open in a fitted full-screen viewer; chat scroll survives focus/window changes.
- [ ] GIF search uses the connected instance key. Oversize carrier media offers a Blossom link.

## 2. Messages and Concord

- [ ] One Messages app contains Direct Messages and Communities; neither tab can pop out after a
      monitor move, invite, focus change, or restored session.
- [ ] Room/community recovery, Armada history, icons, metadata, public discovery, invitations, and
      relay membership converge across fresh sessions and devices.
- [ ] Room entry/join/change lands on latest through delayed history/media; returning preserves a
      deliberate user scroll. Uploads, previews, links, focus, and deletion do not move it.
- [ ] No duplicate optimistic/relay-echo messages or attachment sends.
- [ ] Armada-compatible mentions/autocomplete, mention notifications, reply-to-original navigation,
      reply participant tagging, reactions, deletion, moderation, profile/DM menus, leave, starring,
      editable visibility/description/icon, calls, copy/paste images, Blossom files, and Webxdc.
- [ ] Desktop uses the full Matrix/Discord layout with persistent optional member pane; mobile uses
      a clean collapsible room/channel drawer, hidden members by default, and no horizontal overflow.
- [ ] Attachments and Webxdc render/play/open like post media. Unread rooms are visibly bold.

## 3. Blossom and file opening

- [x] All encrypted folders/files and sync manifests recover without destructive zero-entry writes,
      missing partial listings, lost folders, or stale signer state.
- [ ] Restored file/folder appearance and icons remain intact.
- [ ] Open-with supports PosterChan Code for every suitable file, `.conf` and `.csv`; PDFs use Preview;
      office documents use Office. Cancel/open never leaves a black window or splits Classic/Desktop.
- [ ] Folder upload completes, refreshes, and is visible in the expected Blossom folder.

## 4. Android shell and media

- [ ] Launcher remains visible when PosterChan backgrounds; no inert background-only APK.
- [ ] Double-tap Home and double-tap Social reliably refresh the configured home timeline at top
      without shaking, exiting, or breaking alternate home timelines.
- [ ] Music survives Home/background and exposes compact Shuffle/Refresh/Delete All plus track count;
      move Bluetooth autoplay preference to Phone settings.
- [ ] Terminal opens at current output; user scroll-up is respected, resize returns to bottom only when
      pinned, and Ctrl+PageUp/PageDown changes tabs.
- [ ] Mobile Preview renders PDFs when possible and offers a useful native fallback otherwise.

## 5. Window manager — release blocker

- [ ] No PosterChan or native app ever becomes an empty/black managed window after open, close, quit,
      cancel, focus, minimize, resize, monitor handoff, restart, snapping, or Ctrl+Alt+Backspace.
- [ ] Preview auto-clean/close never exits PosterChanOS Desktop or exposes Classic mode; preserve the
      owning desktop window, route and focused app, with an installed runtime regression test.
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

## 6. Remaining desktop applications

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

## 7. Release gates and ISO — only after sections 1–6

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

## 8. Post-stability polish — only after the backlog above is empty

- [ ] Add PosterChanOS compositing with modern shadows, transitions, smooth movement and restrained
      transparency/blur; include a low-power/off setting and regression/performance tests proving it
      cannot cause black windows, focus errors, input lag, or cross-monitor state loss.
- [ ] After the desktop is stable, add an optional macOS-style PosterChanOS desktop experience in
      Settings. Keep the current experience available, and cover switching, persistence, windows,
      focus, multi-monitor behavior, and rollback with the same no-black-window release tests.
