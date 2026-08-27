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
| Native window ownership | External/native windows use unified focus and placement handling. On final installed 1.0.980, the real Telegram frame restored/focused its XWayland surface; clicking an overlapping PosterChan window then stashed Telegram in the compositor and focused the shell, so it could not remain on top. A real title-bar drag handed the same Telegram con_id left → right → left across renderer processes with exactly one paired frame and no HTML/Social window. A quick edge drag displayed the snap ghost, snapped both the HTML frame and Telegram surface to the right half at matching widths, and restored its prior geometry/state afterward. All three installed checks were rerun after packaging, then the diagnostic instance was stopped and the canonical launcher restored | `check_installed_native_focus.py`, `check_installed_native_handoff.py`, `check_installed_native_snap.py`; exact-package runtime pass plus 59 focus/WM tests, 33 handoff tests with 5 subtests, and 31 snap/native tests |
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

- Exercise an Armada-created public community end-to-end with two identities: discovery, join,
  history, icon and members are live-verified; send, reply/react, mention notification, attachment,
  Webxdc and moderation still require a non-production test community or permission to create
  durable relay events.
- Install APK 1.0.1682 on a physical phone and repeat background Music-after-Home, launcher,
  double-Home, narrow portrait, landscape, tablet and mail-attachment taps. The exact-commit API-34
  emulator suite is green, but an emulator is not a physical-device result.
- Boot `posterchan-live-20260826.iso` from physical USB. Its structure and host-CPU KVM boot are
  verified; QEMU's legacy `qemu64` CPU does not meet this Gentoo image's ISA requirement.
- Exercise real carrier SMS/MMS send/receive and the complete device-provider migration on a phone
  containing representative long history and media. Emulator/provider tests do not prove a carrier.
- Exercise copy/paste specifically through editable fields in native Firefox and XWayland Telegram
  in a sustained manual session. Installed Desktop 1.0.980 now publishes PosterChan text to the real
  Wayland selection and reads an independently owned Wayland selection in the opposite direction;
  both directions passed on the two-output desktop and cleanup left no clipboard daemon or inherited
  listener. Installed two-output shell reconstruction, Music handoff, real Telegram focus/stacking, reversible
  cross-renderer title-bar drag and real edge-preview/snap are verified.
- Reconstruct any substantive additions named in the earlier stash handoff from commits, release
  payloads, or external evidence. The authoritative worktree has no stashes now, so their former
  contents cannot be recovered or declared present merely from `git stash list`.

## August 24 release ancestry

The current branch contains 162 commits dated August 24. Every one is an ancestor of release commit
`2c2a8af1f528e8fb8be898d6f39821b3fba6f32c`, which produced Desktop 1.0.980 and Android 1.0.1682.
The later commits contain the immutable Gentoo overlay bumps, live-ISO boot correction, this audit,
release-gate scripts and tests; they contain no newer Desktop/Android implementation absent from
those artifacts. The live-ISO correction is packaged in shell 1.0.20260826083717.
Ancestry proves inclusion, not behavior, so the runtime/package evidence below remains the authority
and the hardware/external items above remain open.

A current cross-area recovery run selected every Python suite whose path covers SMS/MMS, Files,
Blossom, Code, Music, native windows, Settings/LiveUSB, signer/extension, Social/feed, Preview,
Remote Desktop or Concord. It passed 1,164 tests plus 46 subtests in 53.51 seconds. This complements
the complete release gate; it does not close the physical/carrier/external-application checks above.

## 2026-08-25/26 verification pass

This is evidence from the recovery pass, not a declaration that the whole queue is closed.

| Requirement | Current evidence | State |
|---|---|---|
| Full source/runtime suite | `scripts/checkall.py` after `854ac9dd`: 4,011 unit + 1,615 client tests and 49 browser/runtime checks passed with zero failures; installed-account CDP and OS-back-with-repository-fixture were the two explicit skips; CSS scale reported 498 advisory values. Both skips were rerun with their required environment: installed account passed as recorded below, and `check_os_back.py https://poster.place` opened a real NIP-34 issue in its own window, returned to the repo Issues tab with no leaked window, then returned from a timeline post to the same card at 303px (scroll 900 → 900). `check_os_back` is now explicitly registered in the live group instead of silently contacting production from the self-contained UI group | The original run's two skips are closed by explicit reruns; future full runs retain the CSS advisory |
| Files → Code on a cold session | `91ebe39d`; `module_loader_sim.js` executes the asynchronous loader; drive, synced and host routes all await it | Fixed and packaged in Desktop 1.0.962 |
| Code folder/Git/diff/restore | Final installed Gentoo Desktop 1.0.980 drove a disposable host Git repository through the real `pcHost` bridge and Code UI: selected root retained, modified `changed.js` appeared in Source Control, its real patch rendered, the UI's actual in-app confirmation accepted per-file Restore, the changed row and stale diff disappeared, disk bytes were restored, and Explorer reopened the same working directory. `check_installed_code.py` now makes the installed interaction repeatable while preserving the user's prior Code state; `check_code_editor.py` retains isolated browser/layout coverage | Final installed interaction verified |
| Code/Terminal focus sizing | `check_installed_code_focus.py` drove final installed Desktop 1.0.980 through Code → Terminal → Code using real desktop windows. With Terminal focused, parked Code retained `feed-code` and not `feed-term`; after refocusing Code, parked Terminal retained `feed-term` and not `feed-code`, while the live Code feed remained exclusive. The diagnostic process was stopped and the canonical launcher restored afterward | Final installed focus/sizing regression verified |
| Multi-monitor black output | Live Gentoo 1.0.969 failure had two active/powered 3840×2560 outputs but only one PosterChan surface; the process log proved a diagnostic launch lacked `SWAYSOCK`, `repairPointerGaps()` rejected out of startup, and companion reconciliation never ran. Restarting through `pc-shell-start` restored one visible renderer on each output. `WM` now discovers and proves live sockets in the user's runtime directory when the environment is absent, tries newest candidates instead of trusting a stale filename, and pointer-gap repair cannot abort display reconciliation. 122 focused tests plus five subtests pass | Live failure reproduced and recovered; new immutable Desktop/Gentoo artifact and installed recurrence test required |
| Desktop payload/delivery | Desktop 1.0.969 immutable tarball and public overlay agree at 152,142,378 bytes; the exact Gentoo package is installed and its ASAR contains the server-bound mail attachment and compact mobile toolbar markers. Earlier installed Code/Concord checks and two visible 3840×2560 surfaces remain recorded against 1.0.967 | 1.0.969 delivery/ASAR verified; full installed interaction remains open |
| Email mobile reader | `94d4ae9a`; real Chrome at 360×780, 390×844 and 1280×860 rejects localhost attachment URLs, action wrapping, toolbar height above 54px, clipping and horizontal overflow; 35 focused mail tests pass | Web live; Desktop 1.0.980 installed; signed Android 1.0.1682 published; emulator/physical attachment tap remains open |
| Live production group | Against `https://poster.place`, URL Reading first exposed a nondeterministic grounding failure. `92c234fa` now treats fetched pages as untrusted quoted data, ignores embedded instructions, and requires concrete cited details. The post-deploy group had 13 passes/zero failures and one QR skip; URL Reading then passed standalone and the complete QR clock-skew/idle/two-app/reload/sign run passed standalone. The QR gate now classifies a loaded client rejecting nsec as FAIL, never SKIP | All 14 production scenarios green across the post-deploy group plus explicit rerun; keep future skips visible |
| Armada invite join | A clean second identity joined the installed account's real Soapbox invite without posting: durable community id, immediate hydration, 13 channels, icon, 200 history messages and 28 members. `concord_runtime.mjs` now executes the rendered Ctrl+Enter, quick-reaction, threaded reply and own-message delete controls and verifies CORD kinds 7, 1111 and 5 plus target/participant tags and local deduplication; the 50-test Concord/relay/route set passes | Live read/join and isolated write-action semantics verified; live two-identity send/reply/react/mention/moderation remains open |
| Android background music | Current signed APK 1.0.1682 is 20,353,439 bytes, SHA-256 `421049ace3d92be1d044bf3cf4f5fa7ec1cb71ac2181d3d5078dd9c3dbaff220`, signer SHA-256 `ED:DF:3A:79:83:DF:49:22:1A:5A:CE:0D:0C:A5:2C:89:9D:34:EB:88:A4:15:5B:08:29:B0:5C:0A:FC:31:F3:42`, built from `2c2a8af1`. The exact-commit API-34 run 32945394651 completed all 70 device tests with zero failures/skips, including Music-after-Home; launcher lifecycle/double-Home and drawer tests also passed | Exact release commit, emulator and signed payload verified; physical phone remains open |
| Office service | Installed Gentoo 1.0.979 created a valid temporary ODT, attached CDP to Collabora's out-of-process iframe, required a complete non-read-only workspace with real controls, entered text through Collabora's editing bridge, invoked Save, fetched the resulting ODT through WOPI, and found the inserted text in its `content.xml` before deleting the session. The same run retained exact Files/sync counts; `check_installed_desktop_account.py` makes the path repeatable | Installed interactive edit/save path verified |
| Blossom/synced folders | Installed Gentoo 1.0.969 rendered 29 folder tiles, 30 folder entries and two synced roots with no overflow/runtime errors. The hardened installed-account gate independently pulled the authenticated server pointer and compared its privacy-preserving count with the installed client's decrypted index: 5,982 server entries = 5,982 client entries. It then compared each real synced root at all three layers through the packaged native bridge: 11,954 server = manifest = local with zero skipped, and 5,818 server = manifest = local with zero skipped. The output emits no pair labels, paths, filenames, hashes, URLs, keys or contents. The complete folder/sync regression family passed 418 tests plus 34 subtests. Full sync loop and two-fresh-device checks also pass | Installed Files-index and per-root server/manifest/device completeness verified |
| Old stash handoff | `git stash list` is empty in the authoritative worktree | No stash exists to reconcile; do not claim its former contents recovered from this fact |
| Multi-monitor shell recurrence | Installed Desktop 1.0.977 was launched with `SWAYSOCK` and `I3SOCK` absent. It discovered the live compositor and independently recreated exactly two 3840×2560 surfaces. Installed Super+Right left both shell surfaces at their full output dimensions. Desktop 1.0.979 then moved a real Music window between the two renderer processes: the source closed, and the destination contained the full Music library (`#ma-lib`, 744 controls), not Social or a black frame. `7bf58ac6` maps the internal `doc:music` key back to its reconstructible `__music` launcher and suppresses stale page routes; `test_monitor_handoff_reopens.py` and the real two-renderer check cover both layers | Installed recurrence and handoff verified on the two-output desktop |
| Desktop 1.0.979 delivery | Desktop workflow 32934233192 passed Linux, Windows and macOS builds plus the bundled Concord audit. The immutable tarball is 152,151,912 bytes; the public Gentoo overlay, Manifest and ebuild agree. Both the desktop and laptop run `app-misc/posterchan-desktop-1.0.979` and `app-misc/posterchanos-shell-1.0.20260826053253` | Published and installed on both test machines |
| Desktop 1.0.980 / native clipboard | Desktop workflow 32945394649 passed Linux, Windows and macOS for `2c2a8af1`; Android build 32945394635 and exact-commit API-34 emulator run 32945394651 also passed. The immutable Linux tarball is 152,151,449 bytes and the public Gentoo overlay/Manifest agree. Both desktop and laptop run Desktop 1.0.980 plus shell 1.0.20260826083717. On the installed desktop, packaged `app.asar/clipboard.js` published text to `wl-paste` and read an independently owned `wl-copy` selection; clearing it left no `wl-copy` process or listening socket | Published, installed and native Wayland bridge verified; exact Firefox/Telegram field UX remains manual |
| Installed 1.0.980 account gate | Authenticated installed Electron rendered 29 folders and 30 entries; server and client indexes matched at 5,983 files. Both registered sync roots matched server = manifest = local (11,954 and 5,820) with zero skipped. Office/WOPI returned HTTP 200; the gate attached to Collabora's iframe target, found a complete non-read-only workspace with controls, and proved an editor-entered string persisted inside the saved ODT. The one-off loopback-only diagnostic launch was then stopped and the canonical no-CDP shell restored | Passed against the final installed package |
| Post-deploy full gate | First `./test.sh --live https://poster.place`: 4,024 unit tests, 1,615 client tests, and 62/64 browser/live checks passed. The two red checks reproduced clean alone: profile/search stability passed 20/20 cold flows and QR device login passed clock skew, idle socket, two-app and reload/signing scenarios. The repeat produced 4,026 unit passes, 1,614 client passes with one transient skip, and 63 browser/live passes with zero failures: isolated QR and stability were green, while Full Sync and the installed-account gate skipped. `0593d11b` registers Full Sync as an isolated live check and preserves the installed gate's external port through discovery; runner-driven focused reruns then passed Full Sync (65s) and installed Files/Office (11s), both with zero skips. The client suite was rerun with skip reasons enabled and passed all 1,615 tests plus 99 subtests. CSS scale remains an advisory (498 values) | All functional gates pass; the evidence is the two complete runs plus explicit clean reruns of every non-pass |
| Clean ISO 2026-08-26 | `/home/pc-5ac337fb7cb82127/livecd/posterchan-live-20260826.iso`, 3,570,515,968 bytes, SHA-256 `29df378fc5fad39f04969ffdee40b429675977bdba520d95e5813827d9dc040a`. It contains Desktop 1.0.980 and shell 1.0.20260826083717. Independent inspection found hybrid MBR/GPT and bootable BIOS/UEFI El Torito entries. The original quiet/Plymouth default reproducibly stalled at switch-root while the diagnostic entry reached first-run; `c99eafeb` removes that unsafe flag from both ordinary modes and `e704439c` prevents shell expansion inside the generated GRUB heredoc. The final generated menu was read back from the ISO and the default entry booted under host-CPU KVM to the PosterChanOS Tor wizard at 1280×800 | Final artifact built and default-menu host-profile boot passed; physical USB boot remains the hardware gate |

## 2026-08-27 current-package verification

- Desktop 1.0.1083 and shell 1.0.20260827175951 are installed on both the dual-monitor desktop and
  laptop from the public Gentoo overlay. The immutable tarball checksum was verified before the
  overlay was published; the diagnostic shell was removed afterward and the canonical no-CDP shell
  restored.
- The installed account gate rendered 29 File Manager folders and 6,000/6,000 account files. Its two
  real sync roots matched relay = decrypted manifest = native disk scan at 11,954 and 5,833 entries,
  with zero skipped files. This run caught and fixed both second-resolution relay page truncation and
  a delta cache that could retain stale records indefinitely; full re-anchoring is now periodic.
- The same installed run loaded Collabora with HTTP 200, accepted real editor input, saved through
  WOPI, and proved the resulting ODT contained the edit. The disposable Office session was deleted.
- Installed PosterChan Code selected a disposable local Git repository, showed its modified file and
  real diff, restored it through the UI, returned to a clean tree and Explorer, and retained correct
  full-height Code/Terminal sizing across focus changes.
- Folder/paging regressions passed 122 tests plus three subtests; current File Manager/Office tests
  passed 53 tests; Android launcher/background-Music tests passed 149 tests plus 17 subtests. Signed
  APK 1.0.1802 was published from `eb2dea16`; its exact-commit API-34 emulator gate completed
  successfully. Physical-phone playback remains open because no ADB device was attached.
