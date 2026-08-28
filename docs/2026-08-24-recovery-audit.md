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
| Native window ownership | External/native windows use unified focus and placement handling. Installed 1.0.980 proved Telegram focus, snap and same-con-id cross-renderer handoff. Later focus work stopped treating an ordinary overlapping managed window as a reason to park native pixels; when parking is explicitly required, the frame retains a bounded captured preview or a branded nonblack fallback instead of an empty black body. Installed checks now require an explicitly disposable app identity/PID rather than selecting an arbitrary Firefox/Telegram window | `check_installed_native_focus.py`, `check_installed_native_handoff.py`, `check_installed_native_snap.py`, `test_native_stash_fallback_runtime.py`; historical 1.0.980 interaction plus current package/runtime gates |
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
- Install current signed APK 1.0.1834 on a physical phone and repeat background Music-after-Home, launcher,
  double-Home, narrow portrait, landscape, tablet and mail-attachment taps. The exact-commit API-34
  emulator suite is green, but an emulator is not a physical-device result. On 2026-08-27 no device
  was visible through USB ADB, wireless-ADB mDNS, or the known LAN hosts, so this gate remains open.
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

## 2026-08-27 Android exact-head gate

- Commit `d464795123e9b09c33e7bb1b2dc3d7d71611380b` passed Android emulator run
  `33126267471`: **74/74 tests**, zero failures, errors, or skips; the report records
  `ConcordNotificationDeviceTest.roomChannelAndMessageSurviveTheNotificationTapIntent` (0.027s),
  `MusicBackgroundDeviceTest.aPlayingWebViewTrackKeepsAdvancingAfterHome` (4.758s), and
  `tabletDesktopStateSurvivesHomeAndRotationInBothTasks` (10.554s). Both the device-check step and
  artifact uploads completed successfully.
- APK run `33126267477` at the same full commit published GitHub/Zapstore version `1.0.1834` and
  linked certificate `eddf3a7983df4922…` to the developer npub on all **3/3** configured relays.
  The independently downloaded rolling APK is 20,407,826 bytes, SHA-256
  `e93b300ec2e75b250ed7f77fbb4f1c58670f5a2f55a292e260cd9145fe9eb94e`; APK verification reports
  package `place.poster.app`, versionCode/versionName `1834`/`1.0.1834`, one signer with certificate
  SHA-256 `eddf3a7983df49221a5ace0d0ca52c899d34eb88a4155b0829b05c0afc31f342`, and verified v2/v3
  signatures. Release body provenance names the same full commit. The packaged
  `assets/public/static/js/client/concord.js` contains the Vector interoperability markers:
  `wss://nostr.computingcache.com`, the unfiltered kind-33302 `limit:64` fragment query, and
  `decodeMembershipLists`; this is payload evidence, not merely a source-tree assertion.
- Commit `71c1e9066445cc3f403d429fd2a7d860b234034b` passed Android emulator run
  `33124229201`: **74/74 tests**, zero failures, errors, or skips; the shell device gate and
  instrumented gate both returned zero. The report records
  `ConcordNotificationDeviceTest.roomChannelAndMessageSurviveTheNotificationTapIntent` (0.001s),
  `MusicBackgroundDeviceTest.aPlayingWebViewTrackKeepsAdvancingAfterHome` (5.357s), and
  `tabletDesktopStateSurvivesHomeAndRotationInBothTasks` (11.163s).
- APK run `33124229312` at that exact commit built and published Zapstore version `1.0.1830` and
  reported certificate publication accepted by all three configured relays. Because `apk-latest`
  is rolling, exact-commit rerun `33124370945` subsequently replaced it with `1.0.1831`. The
  currently downloadable APK is 20,406,889 bytes, SHA-256
  `67cf7dc25edf62f565d981caf3a68b92e3cc70825ffea227f2736c258c9263de`; APK verification reports
  package `place.poster.app`, versionCode/versionName `1831`/`1.0.1831`, one RSA-2048 signer with
  certificate SHA-256 `eddf3a7983df49221a5ace0d0ca52c899d34eb88a4155b0829b05c0afc31f342`, and verified v2/v3
  signatures. The rolling release provenance names the same full commit.
- These CI/package results do not close physical phone/tablet, Bluetooth playback, real carrier
  SMS/MMS/APN, or cross-device notification delivery gates.

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

| Recovery requirement | Current repeatable evidence | Remaining boundary |
|---|---|---|
| Files / Blossom opening | `check_installed_files_open_with.sh` executes the immutable ASAR's open-with, folder-drop/upload, native-host and Preview routes; `check_installed_native_files.py` drives the packaged native bridge | Authenticated index/sync completeness remains in `check_installed_desktop_account.py` |
| Office document workspace | `check_installed_document_apps.sh` executes the immutable Office/Email workspace and attachment routing; the account gate performs real Collabora WOPI edit/save/readback | Requires an authenticated diagnostic CDP launch for live WOPI evidence |
| Code Git/diff/restore/focus | `check_installed_code_package.sh` executes immutable native Git restore and Code browser behavior; `check_installed_code.py` and `check_installed_code_focus.py` retain disposable-repository and focus sizing coverage | Interactive gate requires a disposable repository and diagnostic CDP launch |
| WM black-output / monitor coverage | `check_installed_shell_surfaces.py` requires exactly one visible, full-geometry, package-backed shell surface on every active output. Desktop 1.0.1095 passed with two 3840×2560 outputs and two surfaces | Cold start without inherited `SWAYSOCK` remains the disruptive recurrence gate |
| Native focus/handoff/snap | Installed focus, handoff and snap scripts bind actions to exact compositor and renderer identities | Disposable Firefox/Telegram windows remain required; never target an arbitrary user's window |

### Desktop 1.0.1106 / shell 1.0.20260827221110 reconciliation

This is the requirement-by-requirement state at the current package boundary. “Included” means the
immutable payload contains the tested implementation; it does not silently promote an older or
browser-only interaction into a current installed pass.

| Release-gate requirement | Verified through current package | Still open |
|---|---|---|
| Concord behavior | Source/browser room, send, reply, reaction, scroll and responsive suites remain green. Isolated authenticated 1.0.1106 drove the real Messages → Communities tab: Concord rendered, remained snapped/maximised with a 16px frame gutter, and filled its body to 1px | Live two-identity send/reply/react/mention/moderation/attachment/Webxdc remains external |
| Responsive Concord | Browser phone/tablet/desktop sizing and managed-window fill have deterministic coverage | Physical phone/tablet rotation and the complete current-package navigation matrix |
| App isolation | Ownership tests cover lazy modules, route replacement, focus preservation and Terminal/Social snap geometry; Concord is kept inside its owning Messages frame | Exhaustive installed 1.0.1106 click/focus/scroll sequence across every named app |
| Files / Blossom | Authenticated isolated 1.0.1106 matched 6,005 server and client entries. Its two mapped roots each matched relay = decrypted manifest = native scan (11,954 and 5,834) with zero skipped. Disposable native Files listed both fixtures, offered Code and host for `.conf`, and opened SVG in Preview | Physical-device Files behavior and unrelated external storage providers remain separate gates |
| Code | Isolated installed 1.0.1106 selected a disposable Git repository, rendered its changed row and real diff, restored through the UI, returned to a clean tree and Explorer, and retained exclusive full-height Code/Terminal content across focus | User-selected non-disposable repositories remain outside automation by design |
| PosterChanOS / native windows | 1.0.1106 includes atomic handoff, nonblack stash fallback and the zero-screen-coordinate left-snap drag fix. Local exact Terminal-left/Firefox-right coverage passed 1600/1280/1024, three cycles each; native probes now require a disposable app ID/PID | Final installed disposable Firefox/Telegram focus, preview, restore and cross-output repetition on 1.0.1106; do not infer this from ASAR markers |
| Texts / Social | SMS resume/attachment and Social route/scroll-state suites remain covered. The 1600px Social wheel red was a headless verifier defect: the real wheel reached the exact writable feed unprevented; the corrected full desktop matrix passes | Carrier SMS/MMS and live offline/reply/thread preservation remain external gates |
| Release propagation | Desktop workflow 33121179501 succeeded for Linux/Windows/macOS and publish at target `1cfd14462`; immutable 1.0.1106 Linux asset and Manifest both report 152,164,017 bytes. The desktop reports Desktop 1.0.1106 and shell 1.0.20260827221110 | Current web revision, physical Android installation, carrier/external services and physical USB boot remain separate gates |
| Installed account gate | Isolated authenticated 1.0.1106 passed Files/sync completeness and native Files/Preview. Collabora returned HTTP 200, accepted real editor input, saved through WOPI and preserved the edit in the resulting ODT; the disposable Office session was deleted | No current-package account gate remains; future releases must rerun it |

- The dual-monitor desktop currently reports Desktop 1.0.1106 and shell
  1.0.20260827221110 from the Gentoo packages. `/opt/posterchan/resources/app.asar` is 15,615,681
  bytes, SHA-256 `cbc81f3a3e8c2c55886cd4d04d41d5fb37614c265735a0722efa4befeb3ede73`, and is owned by
  the Desktop package. Read-only marker checks found the zero-screen-coordinate Terminal drag fix,
  the nonblack native-stash fallback and File Manager Locations implementation in that installed
  ASAR. This proves installed payload identity, not that every interaction below was rerun on 1.0.1106.
- The final authenticated interaction used the installed binary with a private temporary profile and
  loopback-only CDP. Only the small authentication/state stores were copied; caches and account
  contents were not emitted. The exact diagnostic PID was parked in Sway's scratchpad while the
  canonical process continued running. It passed the Files/sync, native Files/Preview, Office/WOPI,
  Code Git/diff/restore/focus, ten-page Settings and Concord managed-workspace checks recorded in the
  table above. The disposable Office session and Git/files fixtures, diagnostic process/profile and
  local tunnel were removed. A final compositor read found only canonical con_ids 10 and 11, both
  visible and non-scratchpad, one full 3840×2560 surface on each physical output. No Firefox or VM
  was targeted: final disposable native focus/handoff/preview, explicit disposable-VM lifecycle,
  physical phone/tablet/USB, carrier and external two-identity gates remain open.
- Earlier Desktop workflow 33100871080 completed successfully at `eb2dea16`. Both the rolling release and
  immutable `desktop-v1.0.1083` release expose the same 152,166,782-byte Linux tarball, SHA-256
  `9ef5c0d19148f4e309404f0a3ae9099435a630710d200e7d8592544fd86f83dd`. The public overlay head is
  `b13fd0d4991212f264b53b22375d653a84694d91`; its 1.0.1083 ebuild points at the immutable tag and
  its Manifest records the matching size and SHA-512. Both test machines report Desktop 1.0.1083
  and shell 1.0.20260827182039, with `/opt/posterchan/resources/app.asar` owned by the Desktop
  package. The Files/Office/Code interaction itself was run on the earlier shell
  1.0.20260827175951 baseline; subsequent shell-only updates did not replace the Desktop payload.
- Admin Relay → Preview auto-clean completed its real nondestructive count inside an isolated,
  authenticated installed 1.0.1084 shell. During that request, forcing the renderer through the
  narrow-width value produced by display reconciliation reproduced the reported failure exactly:
  `PCOS.isOn()` changed true → false and `#os-root` disappeared. The real OS is the compositor
  session and has no usable Classic fallback, but `enter()` and `onResize()` applied the optional
  browser/tablet width rule to it anyway. They now exempt only a proven `PCOSShell.available()`
  system shell; web and tablet width gates remain. `check_installed_admin_prune_preview.py` clicks
  the real dry-run control, injects that resize race while it is active, and requires the Desktop
  root, Admin route/host and owning managed window to survive. Immutable-package rerun remains open.
- The installed account gate rendered 29 File Manager folders and 6,000/6,000 account files. Its two
  real sync roots matched relay = decrypted manifest = native disk scan at 11,954 and 5,833 entries,
  with zero skipped files. This run caught and fixed both second-resolution relay page truncation and
  a delta cache that could retain stale records indefinitely; full re-anchoring is now periodic.
- The same installed run loaded Collabora with HTTP 200, accepted real editor input, saved through
  WOPI, and proved the resulting ODT contained the edit. The disposable Office session was deleted.
- The installed 1.0.1083 ASAR's own `app.js` and `hostfiles.js` were extracted and executed through
  the File Manager routing simulations. Cold-start PDF offered Preview, Office and Code; `.conf`
  offered Code; `.csv` offered Office and Code; encoded Blossom `.conf`, unknown binary and an
  extensionless project file remained inspectable in Code; and clicking a This Computer MP4 reached
  the chooser before invoking the native opener. The reusable installed gate is
  `scripts/check_installed_files_open_with.sh`; 64 focused open-with/Code tests pass.
- An isolated instance of the installed 1.0.1083 Electron package then exercised the native bridge
  and rendered File Manager itself: it navigated to a disposable directory, listed both fixtures,
  clicked a `.conf` file and found the Code/native choices, then clicked an SVG and required the
  real Preview image surface. The diagnostic profile and fixtures were removed without restarting
  the signed-in desktop. `scripts/check_installed_native_files.py` preserves this installed-runtime
  gate and also supports a remote-owned fixture when CDP is reached through an SSH tunnel.
- Installed PosterChan Code selected a disposable local Git repository, showed its modified file and
  real diff, restored it through the UI, returned to a clean tree and Explorer, and retained correct
  full-height Code/Terminal sizing across focus changes.
- Folder/paging regressions passed 122 tests plus three subtests; current File Manager/Office tests
  passed 53 tests; Android launcher/background-Music tests passed 149 tests plus 17 subtests. Signed
  APK 1.0.1802 was published from `eb2dea16`; its exact-commit API-34 emulator gate completed
  successfully. Physical-phone playback remains open because no ADB device was attached.
- The rolling Android asset was independently downloaded after publication: 20,392,197 bytes,
  SHA-256 `da39460b836a81112e245509a813fc88f25089970367e543fb8d907da0930d6c`, version code/name
  `1802`/`1.0.1802`, and signer certificate SHA-256
  `eddf3a7983df49221a5ace0d0ca52c899d34eb88a4155b0829b05c0afc31f342`. APK Signature Schemes
  v2 and v3 verify. The packaged manifest contains `FOREGROUND_SERVICE_MEDIA_PLAYBACK` and declares
  `place.poster.app.music.MusicService` with the media-playback foreground-service type; the DEX
  contains `MusicService`, `MusicPlugin`, and `MusicWidget`. Android build run 33100871091 and
  exact-commit emulator run 33100871059 both completed successfully at `eb2dea16`; the build log
  confirms publication of the same 20,392,197-byte APK to GitHub and version 1.0.1802 to Zapstore.
  This proves delivery and packaged implementation, not playback on physical hardware.
- Shell 1.0.20260827182039 is installed on the desktop and laptop. Its package-owned Foot launcher
  raises Foot's documented delayed-render window for non-atomic streaming TUI updates while keeping
  the bound below one 60 Hz frame. Against the installed package, 40 clear/write bursts produced 43
  frames instead of the stock launcher's 81, eliminating the exposed intermediate repaint in this
  controlled case. A sustained real Codex/Claude session remains open because neither CLI is
  installed on the test machines.
- An isolated installed 1.0.1083 shell and disposable Firefox profile exercised native-window
  focus and edge snapping through the real Sway bridge. Firefox yielded to an overlapping
  PosterChan window; edge preview appeared; the completed right-half snap kept the native surface
  visible and its compositor/frame geometry aligned. The checks also exposed and fixed a stale
  verifier assumption: the packaged app ID is `place.poster.desktop`, not only `posterchan-desktop`.
  The diagnostic processes were removed and the canonical two 3840×2560 shell surfaces returned to
  full size. Cross-monitor handoff remains open rather than being inferred from these narrower runs.
  A follow-up safety audit found that the original probes selected the first Firefox/Telegram when
  several existed. They now require an exact `PC_NATIVE_APP_ID` for multi-window runs, tag native
  frames with their compositor ID, and make the bridge expose the exact shell surface belonging to
  each renderer. The focus/snap checks were rerun against only disposable Firefox con_id 96. The
  Desktop 1.0.1084 and shell 1.0.20260827190130 were then installed on both machines. An isolated
  installed 1.0.1084 process exposed the new exact per-renderer `shellId`; disposable Firefox
  con_id 100 crossed from one renderer/workspace to the other and returned with the same compositor
  identity, one frame, no HTML replacement, no stash, and matching destination workspace in both
  directions. The run also corrected the probe itself to send coherent virtual screen coordinates
  sixteen pixels beyond the renderer edge (the former one-pixel-inside coordinate exercised snap,
  not handoff). All disposable processes were removed and the canonical two 3840×2560 surfaces
  returned to full size. Cross-monitor native handoff is now proved against the installed package.
- The same shell serializes `update-posterchan` before `emaint` touches Portage's Git checkout. A
  live held-lock test proved a second updater remained blocked with no output or overlay access; 110
  updater/profile tests plus 35 subtests pass. This prevents concurrent launches from leaving a
  zero-byte loose Git object and then falsely reporting a stale package as current.

### Final Desktop 1.0.1117 containment and installed interaction evidence

- Desktop workflow 33126883850 published immutable 1.0.1117 from exact commit `bed842aa8`; the
  Linux asset is 152,172,723 bytes. Both authorized Gentoo machines installed Desktop 1.0.1117 and
  shell 1.0.20260827234100. The shell helper was held inert during the Desktop merge, restored only
  by its owning shell package, and then invoked with one exact canonical PID per machine.
- After restart, the dual-output process PID 907620 and laptop PID 1587165 remained unchanged for
  150 seconds. Sway reported exactly two visible 3840×2560 shell surfaces on DP-1/DP-2 and exactly
  one visible 1920×1080 surface on eDP-1; every surface matched its output rectangle and no Error
  window existed. The earlier repeated-restart incident was traced to the installed-bundle watcher;
  `bed842aa8` now requires two stable 30-second mismatch observations, persists the accepted bundle
  identity across processes, ignores the compositor's synthetic first tick and consumes a missing
  helper's asynchronous error instead of opening an Electron error window.
- A loopback-only installed 1.0.1117 diagnostic passed authenticated Files/Blossom completeness
  (6,005/6,005 files; two sync roots with relay = manifest = local counts 11,954 and 5,834), native
  Files open-with/Preview, Collabora HTTP 200 plus interactive WOPI save, Code real Git diff/restore,
  and Code/Terminal focus sizing. A second isolated compositor targeted only disposable Firefox
  con_id 6: managed-window overlap produced a nonblack preview (mean 48.66, variance 6,128.37), the
  same frame restored the same native surface, and the Start-overlay cycle remained nonblack (mean
  178.71, variance 9,720.86, near-black fraction 0.1626). All diagnostic profiles, fixtures,
  processes, private compositors and tunnels were removed; canonical PIDs and surfaces were
  unchanged afterward. User Firefox/VM windows, physical Android/tablet/USB, carrier and external
  two-identity gates were not targeted and remain separate open gates.

## 2026-08-28 current release evidence

This section supersedes earlier uses of “current” only; it does not erase their historical evidence.
This was the packaged boundary when the section was written: commit
`56c2179ed7f74954560bb462a33b5254699c9c71`, Desktop `1.0.1125`, and Android `1.0.1843`.
It is retained as historical evidence and is superseded by the later boundary recorded below.

- Desktop workflow `33130280572` succeeded at the exact release commit for Linux, Windows, macOS,
  bundled-Concord audit, immutable publish, immutable-tag verification and rolling publish. Release
  `desktop-v1.0.1125` targets that full commit; its Linux tarball is 152,178,848 bytes with SHA-256
  `311d4a70e718ef52b9fea77e4771e068c7c54541cf4a43b18a7ba9b18dbd6671`. Commit `afacc3d0`
  pins the overlay ebuild and Manifest to the same size and SHA-512
  `6bf42be9c7e6fc930c530be14d68fddfc3658abd6b81ba73c38ae0e4bd5f711e8b5944ba7318446e84e3c759fb712d7693cde773958e78f829106a32cbbb1a8f`.
  No installed-1.0.1125 interaction is recorded here, so all older installed Desktop evidence remains
  historical rather than proof of this package.
- Android workflow `33130280597` succeeded at the same full commit and published rolling APK
  `1.0.1843`: 20,411,084 bytes, SHA-256
  `46c86aadda9965ac156eb121a97dff84e476d780bf1f2523267e0c473e5ad344`; its release provenance names
  the full commit. Exact-head emulator workflow `33130280574` passed its device script and instrumented
  script (`device=0 instrumented=0`) and the uploaded XML reports **74/74 tests**, zero failures,
  errors or test skips. The workflow's cached-AVD creation step was conditionally skipped; that is not
  an instrumented-test skip. Physical phone/tablet, Bluetooth, carrier SMS/MMS/APN and cross-device
  notification behavior remain open.
- The active right-edge snap report is implemented by `56c2179e`: scaled `screenX` alone can no longer
  steal an edge drop as a cross-monitor handoff, and a rejected HTML-frame handoff commits the requested
  right tile instead of restoring the old floating rectangle. The focused source gate passed 19/19 and
  `scripts/check_os_desktop.py` passed, including scaled left/right and rejected-right-handoff cases.
  This is source/browser and packaged-payload evidence; an installed 1.0.1125 right snap on the affected
  scaled display, plus disposable native Firefox/Telegram focus, preview, restore and cross-output
  repetition, remains open.
- The mobile File Manager attachment picker request is implemented by `c306ecd9`, an ancestor of both
  current packages: small density renders three square columns, non-image previews remain square, and a
  medium-square toggle is exposed. `scripts/check_blossom_picker_mobile.py` passed with 24 cards,
  `smallCols=3`, square image/non-image previews, no overflow or runtime errors, and a working medium
  toggle. A physical Android 1.0.1843 picker interaction and unrelated external-storage providers remain
  open.
- No focused gate run above failed or test-skipped. This audit did not run the full suite, deploy web,
  install either package, inspect a current authenticated account, boot USB, contact a carrier, create
  relay events, or exercise external applications. Therefore the current web revision, exhaustive
  installed app-isolation/navigation/scroll matrix, live two-identity Concord write/notification/
  attachment/Webxdc/moderation flow, Social offline reply/thread/reading-position flow, real carrier
  history/media migration, physical USB boot, native Firefox/Telegram editable-field clipboard session,
  sustained real Codex/Claude terminal repaint session, and every physical-device item above all remain
  open. The former stash contents likewise remain unrecoverable and unverified.

## 2026-08-28 Desktop 1.0.1127 / Android 1.0.1845 boundary and superseding fix

This is the latest evidence audited through repository commit
`bb764dbeda70e1259f861d18b877d328f06650b3`. Desktop `1.0.1127` and Android `1.0.1845`
were built from application commit `e54e90c83486b6bba40338faea249acb026874d7`; they are real,
independently identified artifacts, but they are **provisional and superseded** for the next release.
Commit `bb764dbed` subsequently fixed Messages/Communities and Direct Messages state preservation
during monitor handoff. No evidence in this audit proves that a Desktop, Gentoo or Android artifact
containing that newer commit has completed publication and installed-device verification.

| Requirement | Authoritative evidence through `bb764dbed` | State / remaining boundary |
|---|---|---|
| Desktop artifact | Desktop workflow `33132153653` completed Linux, Windows, macOS and publish for `e54e90c83`. Immutable release `desktop-v1.0.1127` targets that exact commit. Its Linux tarball is 152,187,868 bytes with SHA-256 `98d489be03da2d212687f07110217d3e62db44731293576d40de21eea7496bb4` | Published and identified, but superseded by `bb764dbed`; a new immutable Desktop release is required |
| Gentoo propagation | The public overlay at revision `9d3817ec227eb0717fffd1001551b85df125f4aa` pins Desktop 1.0.1127 and shell 1.0.20260828011706. Overlay resolution/Manifest focused tests passed | Correct for 1.0.1127 only; the post-release Messages fix still requires a new pin, Manifest, public-overlay verification, installation and restart |
| Installed package identity | Both authorized Gentoo machines reported Desktop 1.0.1127 and shell 1.0.20260828011706. Their installed ASAR SHA-256 was `dbdd7c2976018c18039aa4718df3fb922f78dff27cf144e393de5c79641e86fb` | Exact 1.0.1127 identity verified on both machines; this does not prove `bb764dbed` is installed |
| Installed shell/native windowing | The dual-monitor desktop exposed exactly two visible 3840×2560 package-backed shell surfaces; the laptop exposed one 1920×1080 surface. A disposable native probe passed right-edge preview/snap at full height, actual PointerEvent handoff to the other renderer and return with the same native ID, and ten stable snapshots over two seconds. No user Firefox/Telegram window was selected | Verified for installed 1.0.1127. Future package installation must repeat the exact installed handoff/snap gate; sustained user Firefox/Telegram interaction remains manual/open |
| Installed Files/Preview | On both machines, the exact installed package listed disposable native fixtures, offered Code and host choices, rendered SVG in Preview and emitted no runtime errors. The immutable-ASAR package gates also passed Files open-with, folder upload/drop, This Computer video, Preview image/video/PDF/audio, blob cleanup, bundled PDF and generic MP4 paths | Installed disposable path verified for 1.0.1127. Authenticated current-account File Manager index/sync completeness and physical/mobile external-storage behavior were not rerun and remain open |
| Installed Code/Terminal | Exact installed package gates passed native Git diff/staged restore and browser Code behavior. Installed diagnostic focus isolation kept Code and Terminal in distinct windows with mutually exclusive feed classes | Disposable/package behavior verified for 1.0.1127. No claim is made about arbitrary non-disposable user repositories; a newer package must rerun this gate |
| Installed Office/Email | Exact installed immutable-ASAR Office/Email workspace and mobile-email gates passed | This is package-route evidence only. No authenticated 1.0.1127 Collabora/WOPI edit/save/readback was performed, so that current-account interaction remains open |
| News focus/state | Installed 1.0.1127 retained the same News window, feed and focus with one News instance and unchanged `navigationStart` | Verified for 1.0.1127; exhaustive every-app navigation/focus/scroll sequences remain open |
| Android artifact | Android workflow `33132153694` published signed APK 1.0.1845 from `e54e90c83`. The rolling artifact is 20,412,023 bytes with SHA-256 `28a57c12176ee385b9f542226a2ecbfbc9d5f1f9596b4a4fc1f79f528e6ea906`; a local mirror matched that digest and reported version 1845 | Published and identified, but it predates `bb764dbed`; a new APK and artifact-identity verification are required |
| Android emulator behavior | Exact-commit emulator workflow `33132153658` completed 74 tests with zero failures, errors or skips. Its XML records `aPlayingWebViewTrackKeepsAdvancingAfterHome` (4.185s) and `tabletDesktopStateSurvivesHomeAndRotationInBothTasks` (10.982s) | Background playback and tablet lifecycle are proven on API-34 emulator for 1.0.1845. Audible playback after Home on a physical/OEM device, Bluetooth behavior and physical phone/tablet UI remain open |
| Mobile Files layout | Runtime browser checks at 360×780 and 412×915 rendered three and four columns respectively, with square previews, working layout/selection/drawer/scroll interactions and no horizontal overflow; picker checks covered the same compact behavior | Browser runtime geometry verified. Physical Android interaction and external-storage-provider behavior remain open |
| Messages monitor handoff | `bb764dbed` carries the selected Communities tab, community/channel/scroll handoff state, and selected Direct Message peer into a cold destination before first render. `messages_handoff_destination_runtime.mjs` executes the destination sequence; the focused gate passed 72 tests plus seven runtime subtests | Source/runtime regression fixed. It is not release-complete until a new immutable Desktop/Gentoo package is installed on the two-monitor machine and the actual Communities-room and Direct-peer move/return sequence proves no popout, duplicate or state reset. Android/Web artifact propagation must also be identified |

The 1.0.1127 installed run used disposable/private diagnostics and did not attach the canonical
authenticated account to the test harness. Consequently it cannot replace the earlier authenticated
Files-index/sync or Office/WOPI evidence, and it cannot establish current account behavior after the
new Messages commit. Physical Android playback, complete carrier SMS/MMS migration and sending,
cross-device notifications, live two-identity Concord write/moderation/attachment/Webxdc behavior,
external native-app editable-field/clipboard sessions and physical USB boot are still open. A final
ISO must not be called a release candidate until the `bb764dbed` application boundary is packaged,
installed and passed through the relevant current-package gates.

## 2026-08-28 Desktop 1.0.1133 / Android 1.0.1850 current boundary

This section supersedes earlier uses of “latest” or “current”; those sections remain historical
evidence only. The final application boundary audited here is
`d3dba2320a0f08e9f2dcbe4eb491e95cb2ff9654`. The overlay-only follow-up `4ddff065f` pins the
artifact built from that exact application commit and does not change the packaged application.

| Requirement | Current evidence | State / remaining boundary |
|---|---|---|
| Web propagation | The deployed `app.js`, `os.js`, `concord.js` and `code.js` byte-for-byte match the current tree. Their SHA-256 values are respectively `d09a56bf95e8f1d8e9d1f507064dd32d78685cff24a8e4d650787e711f3cfe19`, `ef0006dacd086587db6b1e224c534c44c6ccbf35605d96a4f1462220dd10553d`, `733dbbd2227213f93bbf95f89f6262edbf3a675deeb1c71e2510c107152096c5`, and `8e5f3fff83b027dbf6563fe7363568d9f07051eb12267fc1a7d68957ab9313cd` | Exact checked web payload verified for these four recovery surfaces; this is not an exhaustive live-account matrix |
| Source/client regression gate | The full client suite passed **1,823 tests plus 99 subtests**, with no failure or skip reported | Green at the final source boundary; installed and hardware gates below remain independent |
| Desktop artifact and Gentoo propagation | Desktop workflow `33136589091` succeeded for exact commit `d3dba2320`. Immutable release `desktop-v1.0.1133` contains a 152,202,144-byte Linux tarball with SHA-256 `9aff744623eef98614c23d3d65ac5c288cab91fe071aca7be08003ff47085c22`. The 1.0.1133 ebuild uses the immutable per-version URL and its Manifest records the same size plus SHA-512 `bdec093107198f04d4511c4218bc7fb7b91cce6b3c09ff78faa743410ff671a0d645ee61d922c9e6ecf155c5ac1ba622e8f489446d19c74d5c4859ed82d55f7b` | Immutable artifact and repository overlay pin verified |
| Installed package identity | Both authorized Gentoo machines (`.102` and `.154`) installed Desktop 1.0.1133. Their package-owned ASARs matched SHA-256 `c73c3bd3` at the recorded abbreviated identity | Exact package installed on both machines |
| Messages and Terminal handoff | On the installed two-output package, actual Communities, Direct Messages and Terminal frames each moved left → right → left and retained the selected live tab/state without a popout, duplicate or reset | The requested installed move/return regression is verified; the exhaustive every-app matrix remains open |
| Files / Open With / Preview | Immutable/package Files, Open With, Preview and responsive gates passed against 1.0.1133 | Current package behavior verified by the focused gates. Authenticated current-account Files/index/sync completeness was not rerun in this pass and remains open |
| Code Git/diff/restore | The current immutable native-Git package gate passed Git tree, real diff and restore behavior | Package gate verified. The authenticated installed Code/Terminal focus interaction skipped and remains open; older installed passes are historical, not a substitute |
| Office | The current immutable Office route/package checks remain covered | The authenticated Collabora HTTP-200 plus WOPI edit/save/readback gate skipped and remains open |
| Android artifact | Android workflow `33136589055` succeeded at exact commit `d3dba2320` and published signed APK 1.0.1850. The rolling APK is 20,421,612 bytes with SHA-256 `fbf43761e8b9b965b4ef0dbaaa251a4e3386e793ccb4abbc1d61c8803fc36641`; release provenance names the full commit, and Zapstore publication plus certificate linkage succeeded on all three configured relays | Exact final APK publication and identity verified |
| Android emulator / background Music | Exact-commit emulator workflow `33136589066` was still in progress when this evidence was recorded | Keep the entire exact-commit emulator gate open, including Music-after-Home, until the completed result and test report are inspected. Physical/OEM audible playback after Home, Bluetooth, phone/tablet layout and launcher interactions remain open regardless of the emulator result |
| ISO | The latest proved ISO remains `posterchan-live-20260826.iso`, which contains historical Desktop 1.0.980 rather than this boundary | Build and structurally/virtually verify a new ISO containing the final package, then boot it from physical USB; neither gate is closed |

The authenticated installed-account run reported explicit skips for Office/WOPI and Code focus, so
neither may be promoted from package checks or older installed evidence. Physical Android playback,
carrier SMS/MMS migration and sending, cross-device notifications, live two-identity Concord writes
and moderation/attachments/Webxdc, native Firefox/Telegram editable-field clipboard use, sustained
real Codex/Claude terminal repaint use, unrelated external-storage providers and physical USB boot
also remain open.

## 2026-08-28 post-1.0.1133 recovery audit at `313f11652`

This section is a current-state correction, not a new release declaration. The authoritative branch
head inspected here is `313f1165285f73b305a18789388aa8164056e124`. Desktop 1.0.1136 and Android
1.0.1853 are the newest completed immutable/rolling artifacts, but both were built from
`4b33df6788c3eb6812a46484c7950341a7aa7a07`; the commits after that boundary are source-only until
the workflows already queued/running at this head finish and their payloads are independently
identified. No installed-device result for 1.0.1136 was found in this pass.

| Requirement | Current authoritative evidence | State / missing proof |
|---|---|---|
| Desktop/Gentoo artifact propagation | Desktop workflow `33138798022` succeeded for exact commit `4b33df678`; immutable `desktop-v1.0.1136` targets that commit and publishes a 152,213,063-byte Linux tarball with SHA-256 `37267594149c5738d1d8e8e0aa9c24fea79dadafda82395174a00cd7de4226c7`. Head `313f11652` pins the 1.0.1136 ebuild and Manifest to the same size and SHA-512 `ff23aad1e5e25922c166c62317f207450c0892b280af92d71bee43d5f516867d1e47efcd639d12b804625a857521ae553d201d282294e9ca0c252971ff743afd` | Published/pinned only through `4b33df678`. Installation on `.102`/`.154`, package identity, authenticated Files/Office/Code interaction and every post-`4b33df678` change remain open |
| Code Git tree/diff/restore | `43ab8f9df` reproduces porcelain-v1 `-z` rename ordering (`NEW`, then `OLD`), makes desktop/server status expose the actionable new path once and restores both paths. The source native-Git simulation passes staged and rename diff/restore; 24 focused Git/UI/package tests and the real-Chrome editor/workdir/focus/height harness pass | The strengthened immutable-package gate fails the previously installed ASAR at the rename assertion, as intended. A new package must pass it and then repeat installed Git/diff/restore plus Code/Terminal focus sizing |
| Files selection/action integrity | `5d939fe7b` keeps bulk selection scoped to the visible folder and disables actions that cannot honestly operate on the selected backend; its focused runtime/source tests pass | Source/runtime proof only. Authenticated Gentoo Files/Blossom counts, chooser/actions and installed UI remain open at this boundary |
| Office edit/save/readback | No newer authenticated WOPI interaction was produced after the explicit 1.0.1133 skip | Open. Package-route tests and historical installed saves do not prove the current package/account |
| Concord composer stability | `e5357f6f5` preserved draft/selection across replacement, but emulator run `33138798044` proved that approach still replaced the textarea and lost focus. `1cc97a443` changes background updates to retain the native textarea node and strengthens `ConcordComposerDeviceTest` to require node identity, value, selection, focus and no focus theft | Current source fix is plausible and source tests pass, but the exact-head emulator run `33139397404` was queued when inspected. Keep Android composer behavior and artifact propagation open until that run is green and its report is inspected; physical keyboard/OEM behavior remains open |
| Vector/Armada joined-room recovery | Public metadata for the affected identity exists across the expected relays: newest kind-13302 membership event `ac8b166a…99c0` is present on computingcache, jskitty, ditto and dreamith; older kind-33302 `d=0` fragments remain on ditto/dreamith/Vector Asia. The supplied invite bundle decrypts to the expected community and three bootstrap relays. `beac8de12` accepts seed-only membership snapshots and recursively normalizes fragmented `held_roots`/channel `priors`; focused Vector/invite and Concord runtime tests pass | Self-encrypted membership content was not safely decrypted through an affected installed signer, and no fresh installed client showed the recovered room. Cross-device room visibility/history therefore remains open despite relay and source evidence |
| Direct Messages/Terminal/Preview monitor handoff | `8d01bc0b8` and `a92f96456` add destination-runtime coverage for selected DM and Terminal route ownership. `a7742478b` transfers Preview document state and object bytes across a renderer handoff; focused Preview/runtime tests pass | These commits are later than the completed 1.0.1136 artifact. Actual installed move/return, state, focus, sizing, and Preview-byte verification remain open |
| Android background Music | `747e96437` strengthens the emulator assertion to require the real foreground-service notification while playback advances after Home. Earlier APK/emulator runs prove the weaker playback-time assertion, but emulator runs `33136589066`, `33137419501`, `33138564565` and `33138798044` all failed the newly added Concord composer device test; they are not green whole-device gates | The rolling APK 1.0.1853 is 20,423,408 bytes with SHA-256 `77700836b4e625ad31d81327931eed76ea050573eaff7709fb50a6f406c46a4d` and provenance `4b33df678`, but no complete green exact-commit emulator report covers it. Exact-head emulator, physical audible playback after Home, notification/service persistence, OEM/Bluetooth behavior and physical phone/tablet lifecycle all remain open |
| Final ISO and physical recovery media | The newest previously proved ISO still contains historical Desktop 1.0.980 | A new ISO containing the final Desktop/Gentoo boundary, structural/virtual verification, and physical USB boot are all open |

Highest-risk unresolved gates are therefore: (1) a green exact-head Android emulator report for the
native Concord composer plus foreground Music assertion, followed by signed-APK identity and physical
device playback/keyboard checks; (2) a new immutable Desktop/Gentoo artifact containing every commit
through `313f11652`, installed on both authorized machines and exercised with authenticated Files,
Office WOPI and Code focus/Git gates; (3) affected-account Vector membership recovery through a real
signed-in bridge; and (4) a final ISO built from that installed boundary and booted from physical USB.
Live two-identity Concord writes/moderation/attachments/Webxdc, real carrier SMS/MMS migration/send,
cross-device notifications, external native-app clipboard sessions and sustained real agent-terminal
use remain independent open requirements and are not implied by any row above.

## 2026-08-28 Desktop 1.0.1137 / Android 1.0.1854 recovery boundary

This section supersedes the post-1.0.1133 package statements above. The immutable Desktop and Android
builds use application commit `313f1165285f73b305a18789388aa8164056e124`; overlay-only commit
`0e08106f7` pins Desktop 1.0.1137 and does not change the application payload.

| Requirement | Current authoritative evidence | State / missing proof |
|---|---|---|
| Full client gate | The final source boundary passed **1,832 tests plus 99 subtests** in 470.40 seconds, with no failure | Green for source/client behavior; installed, authenticated, hardware and external-service gates remain independent |
| Desktop artifact / Gentoo | Immutable Desktop 1.0.1137 Linux is 152,211,444 bytes, SHA-256 `6684d496a1143725e4d583566b83c6f55e82917041188d65e67a309ce174c714`. The public overlay pins 1.0.1137 to this immutable artifact | Published and pinned |
| Installed identity | Both authorized machines report `app-misc/posterchan-desktop-1.0.1137`. Their package-owned ASARs are 15,682,003 bytes with SHA-256 `9fc12010db69cc970d3af59fa046e07bd21c1cda8c4cf6e1017b087515928285`; each machine has exactly one canonical shell after the intelligent restart | Exact package identity and stable canonical process count verified |
| Files / Preview | The immutable ASAR independently passed open-with, folder drop/upload, This Computer video, Preview image/video/PDF/audio, bundled PDF.js, generic MP4, maximized neutral layout, blob handoff/cleanup and responsive Explorer gates. A disposable installed fixture passed date column, unified search, Select all/none, nested folder navigation and SVG Preview. The authenticated installed laptop then matched 6,019 server index entries to 6,019 decrypted client entries, exposed both synced roots, and repeated native `.conf` Code/host choices plus SVG Preview | Current installed/package and authenticated-index behavior verified. This laptop had no locally mapped sync root to compare on-disk counts; physical Android/external-provider behavior remains open |
| Code Git/diff/restore/focus | Both installed ASARs passed staged and rename diff/restore. The rename fixture proves porcelain-v1 `-z` NEW/OLD ordering is parsed correctly, the new path appears once, its diff opens, and restore cleans both paths. Browser editor/workdir/focus/sizing also passed | Current package gate verified; arbitrary user repositories remain outside destructive automation by design |
| Office / Email | Both installed ASARs passed the immutable Office/Email route, policy, workspace and mobile layout gates. A loopback-only authenticated run of the installed 1.0.1137 laptop received Collabora HTTP 200, found a writable editor workspace, inserted text through the real editor, saved it through WOPI and read the sentinel back from the resulting ODT | Current authenticated edit/save/readback verified; the disposable Office session was deleted and the canonical no-debug shell restored |
| Messages / Terminal / Preview handoff | Installed two-renderer gates proved Direct Messages remains in its owning Messages frame, DM send retains the open thread, stale Concord cannot repaint Terminal after handoff, a live three-tab Terminal retains its PTY/route, and Preview transfers real bytes and reconstructs one destination Preview without an object-URL leak. A real dual-output Preview titlebar move retained the same image document and focus with one destination host and none left in the source | Current installed recovery behavior verified; exhaustive every-app/user-native-window sequences remain open |
| Vector/Armada membership | The supplied user npub decodes to `5e759c2ca4a4e222ba7af89e6ff315e1d27843fe8bd0a3e7e61e4ba5b1c07326`, exactly matching the identity whose public 13302/33302 membership coordinates were inspected. The current package accepts released seed-only entries, fragmented lists and nested historical roots. Privacy-preserving installed checks on both machines each hydrated four remote CORD rooms without runtime errors | Both active installed sessions were different identities from the supplied npub, so those counts cannot prove this user's rooms/history. Exact-user live hydration remains open |
| Android artifact | Android workflow published signed rolling APK 1.0.1854, 20,424,112 bytes, SHA-256 `39e041e18dca2b3fbe0022442e56a809328fbc236d26a2d183349e83bca07f98`, from the same application commit. Its certificate `eddf3a7983df4922…` was linked to the developer npub on 3/3 relays and Zapstore reported 1.0.1854 published. APK web-asset provenance passed in the exact-head emulator workflow. An independent fetch found `/apk` still serving 1.0.1850; `scripts/refresh_apk.sh` atomically corrected the mirror, and a fresh public fetch then reported version 1854 with the exact 20,424,112-byte size and matching digest | GitHub, Zapstore and public `/apk` artifact identity verified; behavioral acceptance remains conditional on the whole exact-head emulator result below |
| Android emulator / Music / Concord typing | Exact-head run `33139397404` built the debug APK, byte-verified the packaged Concord asset and passed the bundled composer repaint check, then entered real API-34 device checks | The device step was still running when this row was written. Do not accept the APK until its completed XML proves the Concord textarea node/value/selection/focus assertions and foreground Music-after-Home assertions. Physical audible/OEM/Bluetooth and keyboard behavior remain open regardless |
| ISO | The newest proved ISO remains the historical 2026-08-26 image containing Desktop 1.0.980 | A new image containing this final boundary, structural/virtual install verification and physical USB boot are all open |

The current package gates used disposable fixtures and privacy-preserving counts. They did not expose
account filenames, room names, messages, keys or invite material. The diagnostic shell was removed,
its disposable fixture deleted, and one canonical no-debug 1,920×1,080 package-backed shell restored.
These results do not imply the still-open affected-user Vector hydration, physical Android playback/
keyboard, carrier SMS/MMS, cross-device notification, live two-identity Concord write/moderation/
attachment/Webxdc, external native-app clipboard, sustained agent-terminal, or physical USB
requirements.

## 2026-08-28 Desktop 1.0.1141 / Android 1.0.1864 boundary

This section supersedes the 1.0.1137/1.0.1854 artifact statements above. Application commit
`b1dfe10f4d1825d5068786f273095f0778f82d81` is the immutable build boundary; overlay-only commit
`641e7338d` is the immutable Desktop tag. Shell commit `8b71de765` packages the LiveCD publisher,
and overlay-only commit `a1f3b49ca` pins the final Desktop/shell pair without changing client code.

| Requirement | Current authoritative evidence | State / missing proof |
|---|---|---|
| Source regression gates | At the final package/ISO head, the complete client suite passed **1,834 tests plus 99 subtests** in 471.00 seconds. The complete non-client suite passed **4,313 tests plus 473 subtests** in 331.00 seconds, with 15 explicit environment skips and no failure | Green at the immutable source boundary; installed, authenticated and physical-device gates remain separate |
| Concord / Vector NIP-29 | The affected npub decodes to `5e759c2ca4a4e222ba7af89e6ff315e1d27843fe8bd0a3e7e61e4ba5b1c07326`. Its public relay evidence includes an encrypted kind-10009 Armada membership list plus kind-13302/33302 CORD lists. The final client verifies, decrypts and folds kind-10009 membership, queries its exact relays, loads signed NIP-29 metadata/history, and supports authenticated messages, replies, mentions, attachments, reactions and deletion without converting NIP-29 groups into fake CORD rooms. The focused family passed 63 tests | Implementation and public-event discovery are verified. Neither reachable installed session is signed in as this npub, so its private list has not yet been decrypted in a real signed-in runtime and that exact-user gate remains open |
| Desktop artifact / Gentoo | Immutable Desktop 1.0.1141 Linux is 152,222,202 bytes, SHA-256 `f69903ef09ee274a5421875aca56457059912a46f294174faf7aa6c96602859d`; its Manifest records SHA-512 `ace81d3757605d1b9d4ae170feaae2160cc06dcd79d5a324b2d0ac7aa54bc55903e550bbf702c6fccb0fa16c8efd894740afb49ff5a4d0737d58b5fe206fdbfb`. The public overlay exposes Desktop 1.0.1141 and shell 1.0.20260828081837. That shell owns `/usr/local/libexec/posterchanos/publish_iso.sh`, and installed `gentoo.sh` selects it instead of the invalid historical `/usr/scripts` path; 225 focused ISO/profile tests plus 65 subtests passed | Published. The reachable desktop installed both versions and the installed publisher contract passed. The laptop was unreachable and remains unverified |
| Installed Desktop package | The reachable desktop's installed 1.0.1141 ASAR is 15,692,009 bytes, SHA-256 `43da55376c982e517bf10df268fa01f1d16ef4eebac4a45b69e57acc1307e7d5`. Extracting that exact ASAR passed Files/open-with/folder upload/video/Preview/PDF.js/blob cleanup/responsive Explorer, Code staged-and-rename diff/restore/editor focus, Office workspace and Email mobile gates. An isolated authenticated 1.0.1141 instance then matched 6,019 server index entries to 6,019 decrypted client entries; both synced roots matched server = manifest = disk at 11,954 and 5,792 with zero skipped, native `.conf`/SVG routing worked, and a real Collabora edit persisted through WOPI. Installed Code selected a disposable Git tree, rendered its real diff, restored it, returned to Explorer and retained exclusive full-height Code/Terminal sizing across focus. Cleanup restored exactly two visible 3,840×2,560 package-backed shell surfaces | Exact installed immutable package and authenticated interaction verified on the reachable desktop. The laptop remained unreachable and is not inferred from this result |
| Android artifact | Signed Android 1.0.1864 is 20,427,029 bytes, SHA-256 `afc2365da62d7006da60a1f8678071ea450b7e495230100e3033b02f5eaffe93`. GitHub, Zapstore and the public `/apk` mirror serve that exact identity; Zapstore accepted it on two relays while Damus returned HTTP 503 | Artifact identity and publication verified. Exact-head emulator behavior and physical Android remain independent gates |
| Production web | The deployed Concord bundle and source bundle both hash to SHA-256 `4edac0032e2769d51488a1f22ea12076834ef38784e53397f2c4d518f840cdc0` | Byte-identical deployment verified |
| Android emulator / physical device | Final exact-head run `33152177005` built the debug APK, verified bundled web provenance and passed all **77** real API-34 device tests in 187.104 seconds with zero failure, error or skip. The downloaded XML independently records the native Concord textarea draft/selection/focus surviving three whole-workspace repaints (6.298s), a playing WebView track advancing after Home (8.109s), and tablet desktop state surviving Home plus rotation in both tasks (19.127s) | Emulator acceptance is green. Physical audible/OEM/Bluetooth/keyboard testing remains open and is not implied by emulation |
| ISO | Clean `/home/pc-5ac337fb7cb82127/livecd-final/posterchan-live-20260828.iso` is 3,571,752,960 bytes, SHA-256 `be412b33d005e87ea40dae03ec89ac17ccd10ccb10dc55add268882db3a2b777`. Independent inspection found hybrid MBR/GPT plus bootable BIOS and UEFI El Torito entries. Its squashfs contains Desktop 1.0.1141 and shell 1.0.20260828081837; the embedded ASAR has the exact installed SHA-256 `43da55376c982e517bf10df268fa01f1d16ef4eebac4a45b69e57acc1307e7d5`. The source account home and SSH private host keys are absent, and the deliberately present `/etc/machine-id` is empty for per-boot identity. The graphical VM gate passed three consecutive post-grace frames. The VPS independently hashed the complete staged image before atomically publishing it and its sidecar; `https://iso.poster.place/posterchanos.iso` reports the exact 3,571,752,960-byte length and the public sidecar reports the exact digest | Structural/live-VM/publication gates are green. An install to a disposable virtual disk and physical USB boot remain open and are not inferred from the live-session boot |
