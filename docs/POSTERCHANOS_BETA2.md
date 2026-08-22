# PosterChanOS Beta 2

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
