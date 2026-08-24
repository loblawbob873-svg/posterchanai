# PosterChanOS Beta 1 completion record

Beta 1 is a stability cutoff. A checked item must have an automated regression test and must be
present in a published PosterChan Desktop build before it counts as shipped.

- [x] Keep every active monitor usable across login, hotplug, layout changes, and reboot; native
  windows must move between outputs and the saved layout must be restored.
- [x] Restart the desktop safely with Ctrl+Alt+Backspace, and open Task Manager with Ctrl+Alt+Delete.
- [x] Keep native Firefox, Telegram, Steam, terminals, and VM viewers attached to their PosterChan
  frames while opening, focusing, moving, and resizing them; no black background windows. Steam and
  every other native window must retain a working close action and be clamped wholly inside the
  destination monitor after a drag or cross-output handoff—never stranded partly off-screen.
- [x] Keep the Start menu focused and searchable from the Super key over both native and PosterChan
  windows, without also opening Terminal.
- [x] Keep the power, volume, network, Nostr, battery, and community panels inside the usable screen
  at every supported scale and resolution.
- [x] Make Notes, Blossom, Streams, Texts, and every launcher deep link work on the first open without
  a reload, frozen desktop, stale route, or indefinite signer wait.
- [x] Validate native Steam launch, compositor attachment, audio, Vulkan/32-bit dependencies, and
  controller support on the release image. Steam runs directly through Sway/XWayland; Gamescope is
  intentionally optional and is not installed as a workaround.
- [x] Run the installer, desktop-shell, routing, compositor, APK, and Android-emulator release gates
  against the exact commit used to publish Beta 1.
- [x] Add a cyberpunk `posterfetch` welcome to every new interactive Terminal tab, with a PosterChan
  ASCII logo and concise OS, kernel, uptime, CPU, RAM, GPU, storage, network, and session statistics.

Features deliberately deferred from this cutoff live in [POSTERCHANOS_BETA2.md](POSTERCHANOS_BETA2.md).

## Final acceptance

- Non-SMS stabilization suite: 996 passed, 3 skipped, 54 subtests passed.
- Installer/native-window focused suite: 157 passed, 37 subtests passed.
- Browser interaction checks passed for the PosterChanOS desktop, responsive Music player, and Notes.
- Hardware acceptance used two contiguous 3840×2560 outputs. The saved layout survived a shell
  restart; both shell surfaces rendered; Telegram and native Steam opened decorated and wholly inside
  their destination outputs.
- Desktop 1.0.818 and shell 1.0.20260824183713 were installed from the published PosterChanOS overlay.
- LiveUSB hardware acceptance is intentionally tracked outside this list; it was removed after the
  successful live-media test.
