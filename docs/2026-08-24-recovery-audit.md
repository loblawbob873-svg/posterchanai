# 2026-08-24 recovery audit

This file is the durable handoff for the regression-heavy 2026-08-24 work. A source commit is not
enough to mark an item shipped: web/static, Android, Electron, and the Gentoo overlay have separate
delivery paths. Keep an item open until its implementation, regression test, packaged payload, and
live behavior agree.

## Confirmed recoveries

| Area | Current protection | Test / evidence |
|---|---|---|
| Concord cannot replace another app | View ownership is checked before and after lazy work | `tests/client/test_module_view_ownership.py`, `concord_runtime.mjs` |
| Concord desktop package | Built payload must contain CSS, protocol, reader, UI, and nav entry | Desktop workflow `Audit bundled Concord surface`; 1.0.896 ASAR manually audited |
| Concord Android launcher | Dedicated activity alias, tile and adaptive/raster icons | `tests/test_android_concord_launcher.py` |
| Concord mobile/tablet/web width | Full-screen drill-down and shell-gutter rules | `tests/client/test_concord_ui.py` |
| Concord room recovery/send/scroll | Joined-room persistence, optimistic sends and inner scroll memory | `tests/client/test_concord_ui.py`, `concord_runtime.mjs` |
| Blossom file icons | Shipped SVG sprite icons; no platform emoji dependency | `tests/client/test_blossom_file_icons.py` |
| Blossom open-with | Fresh-load preview detection and decoded `.conf` names | `test_preview.py`, `open_with_selector_sim.js` |
| Code local files | Host read/write bridge, atomic save and changed-on-disk refusal | `test_files_open_in_code.py`, `test_code_edits_your_own_computer.py` |
| Code access model | Operators edit node tree; ordinary users get isolated workspaces | `test_code_is_for_everyone.py`, `test_code_editor_api.py` |
| Module/app ownership | Every lazy module is prevented from repainting another view | `tests/client/test_module_view_ownership.py` |
| Webxdc cursor | Pointer lock is explicitly released when a session closes | `tests/test_webxdc_cursor_cleanup.py` |
| IndexedDB cursor safety | App callbacks never await inside a live cursor transaction | `tests/client/test_idb_cursor_never_awaits.py` |
| Texts history resume | Incomplete migration resumes after reconnect | `sms_sim.js`, `test_sms_rescan.py`, `test_sms_attachments.py` |
| Packaged email attachments | Download links bind to the configured instance, never the WebView/Electron origin | `test_mobile_mail_reader.py`, `check_mail_mobile.py` |
| Native window ownership | External/native windows use unified focus and placement handling | `test_desktop_wm.py`, `test_os_native_windows.py` |
| Feed reconnect | Timeline waits for a usable relay and retries one unanswered EOSE | `test_feed_asks_a_socket_that_can_answer.py` |
| Gentoo desktop delivery | Overlay pins an immutable, checksummed release tarball | `test_gentoo_overlay_pins_resolve.py`, `test_sync_updates_desktop_overlay.py` |

## Must remain in the release gate

- Concord: Armada/CORD room discovery, icons, membership, history, send latency, replies, reactions,
  mentions, moderation, public/private state, invite links, attachments, Blossom picker, Webxdc,
  notifications, stable bottom-follow scrolling, and visible member rail.
- Responsive Concord: active communities and rooms remain reachable on classic phone, Android,
  tablet, desktop, and browser; no horizontal overflow, right-side gap, or shell rail collision.
- App isolation: clicking Concord, Texts, Code, Social, Files, Music, Terminal, Settings, or Webxdc
  cannot repaint, resize, focus, or reset scroll in a different window/app.
- Files/Blossom: folders and icons survive signer reconnect; `.conf` opens in Code; PDF opens in
  Preview; local/synced/Blossom files share the correct open-with choices; attachments remain visible.
- Code: working-directory switching, Git tree, changed-file list, clickable diff, local files, and
  built-in `ngit` must work without requiring a separate user install.
- PosterChanOS: native Firefox/Telegram decoration, stacking/focus, edge snap preview, Terminal sizing,
  Music background playback, Remote Desktop user routing, Settings widgets/LiveUSB, Start-menu install,
  and launcher entries must be tested against the installed package—not just source.
- Texts/Social: complete SMS/MMS history and attachments resume without destructive empty-state
  decisions; Social refreshes after offline without discarding a reply/thread or reading position.
- Release propagation: verify the exact web build, Android APK, Electron ASAR, immutable desktop
  tarball, Gentoo Manifest/ebuild, and published overlay revision before calling a change deployed.
- Account-dependent installed desktop gate: run `scripts/check_installed_desktop_account.py` against
  the installed Electron build over a loopback-only CDP port. It must render Files/Blossom without
  exposing account contents and pass temporary Office WOPI write/read plus a real editor HTTP 200.

## Open verification queue

- Run `./test.sh` and record every failure, skip, and advisory result. A skip is not a pass.
- Run the live suite against `https://poster.place` after the static deploy settles.
- Install the newly published Gentoo desktop package in a clean profile and inspect its ASAR/build
  stamp from the installed path.
- Exercise an Armada-created public community end-to-end with two identities: discovery, join,
  history, icon, members, send, reply/react, mention notification, attachment, Webxdc and moderation.
- Exercise phone classic mode and Android at narrow portrait, landscape, and tablet widths.
- Reconstruct any substantive additions named in the earlier stash handoff from commits, release
  payloads, or external evidence. The authoritative worktree has no stashes now, so their former
  contents cannot be recovered or declared present merely from `git stash list`.

## 2026-08-25/26 verification pass

This is evidence from the recovery pass, not a declaration that the whole queue is closed.

| Requirement | Current evidence | State |
|---|---|---|
| Full source/runtime suite | `scripts/checkall.py` after `854ac9dd`: 4,011 unit + 1,615 client tests and 49 browser/runtime checks passed with zero failures; installed-account CDP and OS-back-with-repository-fixture were the two explicit skips; CSS scale reported 498 advisory values. Both skips were rerun with their required environment: installed account passed as recorded below, and `check_os_back.py https://poster.place` opened a real NIP-34 issue in its own window, returned to the repo Issues tab with no leaked window, then returned from a timeline post to the same card at 303px (scroll 900 → 900). `check_os_back` is now explicitly registered in the live group instead of silently contacting production from the self-contained UI group | The original run's two skips are closed by explicit reruns; future full runs retain the CSS advisory |
| Files → Code on a cold session | `91ebe39d`; `module_loader_sim.js` executes the asynchronous loader; drive, synced and host routes all await it | Fixed and packaged in Desktop 1.0.962 |
| Code folder/Git/diff/restore | Installed Gentoo 1.0.969 drove a disposable host Git repository through the real `pcHost` bridge and Code UI: selected root retained, modified `changed.js` visible, patch rendered, per-file Restore confirmed, changed row and stale diff removed, disk bytes restored; `check_code_editor.py` keeps the repeatable browser coverage | Installed interaction verified |
| Code/Terminal focus sizing | CDP against installed Gentoo 1.0.964 drove Code → Terminal → Code and asserted parked Code has only `feed-code`, parked Terminal only `feed-term`, and no slot has both; installed 1.0.965 ASAR retains the gated marker | Installed regression verified |
| Multi-monitor black output | Live Gentoo 1.0.969 failure had two active/powered 3840×2560 outputs but only one PosterChan surface; the process log proved a diagnostic launch lacked `SWAYSOCK`, `repairPointerGaps()` rejected out of startup, and companion reconciliation never ran. Restarting through `pc-shell-start` restored one visible renderer on each output. `WM` now discovers and proves live sockets in the user's runtime directory when the environment is absent, tries newest candidates instead of trusting a stale filename, and pointer-gap repair cannot abort display reconciliation. 122 focused tests plus five subtests pass | Live failure reproduced and recovered; new immutable Desktop/Gentoo artifact and installed recurrence test required |
| Desktop payload/delivery | Desktop 1.0.969 immutable tarball and public overlay agree at 152,142,378 bytes; the exact Gentoo package is installed and its ASAR contains the server-bound mail attachment and compact mobile toolbar markers. Earlier installed Code/Concord checks and two visible 3840×2560 surfaces remain recorded against 1.0.967 | 1.0.969 delivery/ASAR verified; full installed interaction remains open |
| Email mobile reader | `94d4ae9a`; real Chrome at 360×780, 390×844 and 1280×860 rejects localhost attachment URLs, action wrapping, toolbar height above 54px, clipping and horizontal overflow; 35 focused mail tests pass | Web live; Desktop 1.0.969 installed; signed Android 1.0.1672 published; emulator/physical attachment tap remains open |
| Live production group | Against `https://poster.place`, URL Reading first exposed a nondeterministic grounding failure. `92c234fa` now treats fetched pages as untrusted quoted data, ignores embedded instructions, and requires concrete cited details. The post-deploy group had 13 passes/zero failures and one QR skip; URL Reading then passed standalone and the complete QR clock-skew/idle/two-app/reload/sign run passed standalone. The QR gate now classifies a loaded client rejecting nsec as FAIL, never SKIP | All 14 production scenarios green across the post-deploy group plus explicit rerun; keep future skips visible |
| Armada invite join | A clean second identity joined the installed account's real Soapbox invite without posting: durable community id, immediate hydration, 13 channels, icon, 200 history messages and 28 members; `concord_runtime.mjs` covers the transaction in isolation | Live read/join path verified; live send/reply/react/mention/moderation remains open |
| Android background music | Signed APK 1.0.1672 from `94d4ae9a`; API-34 run 32926967998 completed all 70 device tests, including `aPlayingWebViewTrackKeepsAdvancingAfterHome` (10.15s). Its report records PLAYING positions advancing after Home; launcher lifecycle/double-Home and drawer tests also passed | Emulator and signed payload verified; physical phone remains open |
| Office service | Installed Gentoo 1.0.969 created an authenticated temporary `.txt` session, verified WOPI read/write/read, loaded Collabora `cool.html` with HTTP 200, and deleted the session; `check_installed_desktop_account.py` makes the path repeatable | Installed authenticated service path verified; interactive editing controls remain open |
| Blossom/synced folders | Installed Gentoo 1.0.969 rendered 29 folder tiles, 30 folder entries and two synced roots with no overflow/runtime errors. The hardened installed-account gate independently pulled the authenticated server pointer and compared its privacy-preserving count with the installed client's decrypted index: 5,982 server entries = 5,982 client entries. It then compared each real synced root at all three layers through the packaged native bridge: 11,954 server = manifest = local with zero skipped, and 5,818 server = manifest = local with zero skipped. The output emits no pair labels, paths, filenames, hashes, URLs, keys or contents. The complete folder/sync regression family passed 418 tests plus 34 subtests. Full sync loop and two-fresh-device checks also pass | Installed Files-index and per-root server/manifest/device completeness verified |
| Old stash handoff | `git stash list` is empty in the authoritative worktree | No stash exists to reconcile; do not claim its former contents recovered from this fact |
