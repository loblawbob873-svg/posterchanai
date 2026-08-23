# PosterChanOS Beta 2 completion record

The Beta 2 backlog is complete. LiveUSB acceptance is tracked separately and was removed from this
list after successful hardware testing. A feature counts here only when its shipped entry point and
failure-prone behavior have regression coverage.

- [x] SMS/MMS attachment mirroring: carrier retrieval, encrypted `Messages`/`MMS` Blossom storage,
  previews, lazy originals, prompt archive wakeups, and web/desktop rendering
  (`test_android_mms.py`, `test_android_sms.py`, `test_sms_attachments.py`).
- [x] Blossom → This Computer file manager: breadcrumbs/back, selection and select-all, folder
  creation, rename, trash, responsive list/grid views, details, and Share with Blossom
  (`test_host_fs.py`, `test_host_files_view.py`).
- [x] System Settings: displays and persistence, networking, Bluetooth, input/output audio,
  brightness/power, input preferences, users, updates, diagnostics, hibernation, and configurable
  idle/display-off behavior (`test_admin_settings_coverage.py`, `test_desktop_bluetooth.py`,
  `test_desktop_power_audio.py`, `test_displays.py`).
- [x] Guided VM creation and editing: firmware, CPU/RAM, storage/ISO, boot order, networking,
  sound/display, attached hardware, explicit actions, fit/actual/fullscreen viewer modes, and safe
  pointer/window attachment (`test_desktop_vm.py`).
- [x] Persistent per-user taskbar pin/unpin and desktop add/remove actions
  (`test_desktop_taskbar_pins.py`, `test_desktop_layout.py`).
- [x] Cyberpunk, bounded-cost `posterfetch` for new terminal sessions (`test_posterfetch.py`).
- [x] Social pauses timeline subscriptions/rendering while hidden and performs deterministic catch-up
  with preserved position and a new-posts jump on return (`test_timeline_background_pause.py`,
  `test_new_posts_button.py`, `test_need_event_retry.py`).
- [x] Remote Desktop by npub, IP, or `name@host`, with verified Nostr signaling and screen-only guest
  media (`test_remote_desktop.py`).

Release gates still include the complete test suite, Android Java/Gradle build, desktop packaging,
and production deployment verification; those are release procedures, not open feature tasks.
