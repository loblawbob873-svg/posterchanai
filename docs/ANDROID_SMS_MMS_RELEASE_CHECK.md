# Android SMS/MMS release verification

Section 1 of `BACKLOG_BETA3.md` is not complete until this checklist passes against the exact signed
APK being released. Unit/provider simulations prove control flow; they do not prove an OEM provider,
an MMS APN, a carrier callback, Android's camera/document providers, or a second physical device.

## Record the environment

- [ ] APK version and git commit: `________________ / ________________________________`
- [ ] Android device/model and OS/API: `_____________________________________________`
- [ ] Carrier, SIM slot, default data SIM, and reported MMS limit: `___________________`
- [ ] PosterChan is the default SMS app and has SMS/runtime permissions.
- [ ] Connected instance URL, account npub suffix, and Files server host: `_____________`
- [ ] A web browser and PosterChanOS Desktop are signed into that same account.
- [ ] Before testing, save screenshots/counts of the carrier conversation and Files → MMS folder.

## History and media convergence

- [ ] Use a phone containing more than 400 messages, messages older than 30 days, at least two MMS
      in one second, an MMS with caption, a video, and an MMS whose provider part is unreadable.
- [ ] In Texts, run **Bring in older messages** twice. The second run must cross an already archived
      first page; it must not stop before older media.
- [ ] Android, web, and Desktop show the same chronological thread without duplicate bubbles.
- [ ] Web/Desktop initially use thumbnails, open the encrypted original on intent, and truthfully
      label an unreadable/local-only part. Files → MMS contains encrypted originals/previews once.
- [ ] Leave and reopen Texts during migration, disconnect/reconnect the instance, then resume. Counts
      converge without resetting successful work or declaring a capped/refused MMS read complete.

## Send, carrier status, retry, cancellation, and deletion

- [ ] From web and Desktop send one photo and one short video through the phone. Each produces one
      pending bubble, one carrier message, and one final bubble after provider reconciliation.
- [ ] Exercise sent, explicit carrier failure, and ambiguous/timeout callback results. Ambiguous
      status says carrier status pending/delivery unknown and never offers an unsafe automatic retry.
- [ ] Explicitly retry a definite failure. The old receipt disappears only after the new request is
      accepted; one message is sent and no failed/pending ghost returns after refresh or app restart.
- [ ] Queue while the phone is offline, cancel from another device, then reconnect the phone. Nothing
      is sent. Refresh from a deliberately lagging relay/cache; the old request must not resurrect.
- [ ] Delete a sent SMS and MMS on Android: provider and archive copies disappear. Delete remotely:
      only the archive claim is made until the handset can truthfully remove its provider row.
- [ ] Force provider-delete refusal. The UI reports refusal and the archive remains, so rescan cannot
      reverse a claimed deletion.

## Attachment sources and fitted viewer

- [ ] **Camera photo** returns a full-resolution FileProvider JPEG even when the camera returns a null
      result Intent. Send it without a caption and with a caption; both travel as MMS, not plain SMS.
- [ ] **Device** opens Android's readable document picker and accepts image and video content URIs,
      retaining the display name/MIME. Cancel returns to the unchanged draft.
- [ ] **Files** opens the same account/conversation, shows readable names, size, MIME, previews and
      folders, then attaches a file from the configured Files host. Repeat with a host that requires
      connected-instance proxy fallback (no permissive browser CORS).
- [ ] Open an image and video full-screen on Android/web/Desktop. Media fits portrait and landscape;
      native video controls remain usable; closing returns to the exact conversation position.
- [ ] While deliberately scrolled in old history, allow images above and below the viewport to load,
      move/focus the Desktop window, and return. The visible message stays anchored; latest remains
      pinned only when the reader was already at latest.

## GIF and oversized carrier media

- [ ] GIF search succeeds using the connected instance's configured provider key; the packaged APK
      must not query its local bundle origin.
- [ ] Send media below the carrier-reported limit as MMS. Send media above it: Texts offers/sends an
      encrypted Files link, the carrier command contains no oversized attachment, and a clean browser
      can open the link. Confirm the URL fragment (key) is never present in server logs/requests.

## Local evidence recorded on 2026-08-27

- `tests/test_android_shell_compiles.py` plus consolidated section-1 suites: **174 passed** on the
  clean repeat (the broader invocation below completed one additional unrelated test before failing).
- `adb devices -l`: unavailable because `adb` is not installed in this environment.
- `tests/test_android_device_tests_compile.py`: unrelated harness failure; the local
  `ActivityScenario` test stub lacks `getState()` used by `MusicBackgroundDeviceTest`. No SMS/MMS
  compile error was reported before that failure.
