# PosterChanOS Beta 2

- Finish end-to-end SMS/MMS attachment mirroring: encrypt originals and thumbnails on Android,
  store them in the Blossom MMS area, synchronize their records promptly, and render thumbnails in
  the web/desktop Texts client without downloading full-size media until requested.
- Rebuild Blossom → This Computer as a real file manager: clear back/breadcrumb navigation,
  single- and multi-selection, select all, rename, copy/move, delete with confirmation, useful file
  details, and a polished responsive list/grid layout. Keep “Share with Blossom” as a first-class
  action for selected files rather than leaving the host view as a read-only directory listing.
- Rebuild System Settings into a stable, conventional settings app. It must never eagerly load or
  retain unbounded system data, and opening it must not exhaust RAM. Cover displays/layout and
  persistence, Wi-Fi/network, Bluetooth, sound inputs/outputs, brightness and power profiles,
  keyboard/mouse, users, date/time, updates, diagnostics, hibernation, and lock/sleep behavior.
  Expose the Sway idle/display-off timeout with sensible presets plus Never, persist it through
  `pc-idle`, clearly distinguish screen-off from suspend, and apply changes without restarting the
  graphical session.
- Redesign Virtual Machines around a simple guided workflow: New VM asks for OS/ISO, disk size,
  memory and CPU, then creates a sensible BIOS/UEFI and sound/display configuration automatically.
  Each VM card needs obvious Start, Stop, View, Edit and Delete actions; Edit must expose boot order,
  removable ISO media, disks, networking, sound/display and add/remove hardware without requiring
  libvirt terminology. Keep advanced XML/details behind an Advanced section.

Deferred from the Beta 1 stabilization cutoff:

- Pin and unpin apps on the taskbar, persisted per user and synced with the desktop layout.
- Add apps from the Start menu to the desktop, with keyboard and long-press/right-click access.
- Expand System Settings with first-class Displays, Audio/Bluetooth, Power/Hibernation, Users,
  Network, Updates, and Recovery pages.
- Add Recovery & LiveUSB to System Settings: build and validate an ISO, safely identify removable
  drives, require explicit target confirmation, burn it, and report progress and errors.
- Rework the VM viewer to use its available frame instead of letterboxing with wasted brown space.
  Provide Fit, Actual size, and Fullscreen modes while preserving guest aspect ratio.
- Keep the VM viewer attached to its PosterChan frame while dragging and resizing. Recompute the
  guest viewport after every frame move and size change, and never leave it detached or foreground
  locked.
- Put a visible Edit button on every VM and open a polished graphical editor for CPU, memory,
  storage, networking, BIOS/EFI, graphics, sound, USB, boot order, and other attached hardware.
- Simplify the VM editor around a guided Basic view (name, OS, CPU, memory, disk, network and
  firmware), put uncommon devices and raw libvirt controls behind an Advanced section, use plain
  labels instead of empty icon boxes, and keep Save, Cancel and destructive actions unambiguous.
- Redesign `posterfetch` as an exciting polished cyberpunk terminal welcome with stronger PosterChan
  identity and visual hierarchy; keep its system statistics fast, readable, and useful.
- Suspend Social timeline subscriptions and rendering while Social is not visible to reduce network,
  CPU, and battery use. When the user returns, fetch the missed interval, deduplicate and merge posts
  deterministically by timestamp/event ID, preserve the reading position, and expose the existing
  new-posts jump so refresh never scrambles the timeline or silently leaves a gap.
