# PosterChanOS Beta 1 stabilization

Beta 1 is a stability cutoff. A checked item must have an automated regression test and must be
present in a published PosterChan Desktop build before it counts as shipped.

- [ ] Install from the current LiveUSB into an encrypted disk, reboot twice, and reach the graphical
  sign-in without maintenance mode, a recreated welcome flow, or a missing home directory.
- [ ] Keep every active monitor usable across login, hotplug, layout changes, and reboot; native
  windows must move between outputs and the saved layout must be restored.
- [ ] Restart the desktop safely with Ctrl+Alt+Backspace, and open Task Manager with Ctrl+Alt+Delete.
- [ ] Keep native Firefox, Telegram, Steam, terminals, and VM viewers attached to their PosterChan
  frames while opening, focusing, moving, and resizing them; no black background windows.
- [ ] Keep the Start menu focused and searchable from the Super key over both native and PosterChan
  windows, without also opening Terminal.
- [ ] Keep the power, volume, network, Nostr, battery, and community panels inside the usable screen
  at every supported scale and resolution.
- [ ] Make Notes, Blossom, Streams, Texts, and every launcher deep link work on the first open without
  a reload, frozen desktop, stale route, or indefinite signer wait.
- [ ] Validate Steam input capture, audio, Vulkan/32-bit dependencies, and game launch on the release
  image.
- [ ] Run the installer, desktop-shell, routing, compositor, APK, and Android-emulator release gates
  against the exact commit used to publish Beta 1.
- [ ] Add a cyberpunk `posterfetch` welcome to every new interactive Terminal tab, with a PosterChan
  ASCII logo and concise OS, kernel, uptime, CPU, RAM, GPU, storage, network, and session statistics.

Features deliberately deferred from this cutoff live in [POSTERCHANOS_BETA2.md](POSTERCHANOS_BETA2.md).
