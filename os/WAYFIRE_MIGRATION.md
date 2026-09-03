# PosterChanOS Wayfire migration gate

Wayfire is a viable floating-first compositor for PosterChanOS, but it is not a
drop-in replacement for Sway.  The production session must stay on Sway until a
Wayfire backend satisfies every gate in this document.

## Gentoo packages

The supported baseline is Gentoo stable `gui-wm/wayfire-0.10.1` with `X`,
`dbus`, and `gles3` enabled.  XWayland is mandatory for Steam, Proton, and older
applications.  `gui-libs/wayfire-plugins-extra-0.10.0` requires wlroots' 
`x11-backend` flag in addition to the existing `X`, `drm`, `libinput`, and
`session` flags.

Gamescope is `gui-wm/gamescope`, not `games-util/gamescope`.  Its current Gentoo
release is testing-keyworded on amd64, so an image that includes it must carry a
narrow `=gui-wm/gamescope-3.16.25-r1 ~amd64` acceptance rather than unmasking a
category.  Build it with `pipewire`, `libei`, and `wsi-layer`; keep direct
Wayfire launch as a tested fallback if Gamescope cannot initialize Vulkan.

Retain XWayland, PipeWire/WirePlumber, `xdg-desktop-portal`,
`xdg-desktop-portal-wlr`, `xdg-desktop-portal-gtk`, grim/slurp, wl-clipboard,
foot, and swayidle during the migration.  Their jobs do not disappear when the
compositor changes.

## Compositor contract

`desktop/wm.js` currently speaks the i3/Sway binary IPC protocol directly and
depends on Sway's tree schema, container ids, command criteria, scratchpad,
ticks, and window/output events.  A Wayfire backend must provide the same
application-level contract before the session can switch:

- enumerate outputs, including logical geometry, scale, transform, and focus;
- enumerate native and XWayland toplevels with stable ids, pid, application
  identity, title, output, focused/fullscreen/hidden state, and geometry;
- focus, raise, close, move, resize, minimize/restore, fullscreen, and move to
  output by exact id;
- report map, unmap, focus, title, geometry, output, and fullscreen changes;
- deliver PosterChan commands to exactly one shell surface on the active output;
- preserve the shell's per-output ownership and native-window adoption rules.

Wayfire 0.10's IPC framework and `ipc-rules` plugin cover only part of this
contract.  Missing event or window-control operations belong in a small
package-owned Wayfire plugin or adapter.  Do not emulate them with focus timing,
screen coordinates, process names, or repeated shell commands.

Session scripts must become compositor-neutral.  `SWAYSOCK`, `swaymsg`, Sway's
`send_tick`, `/etc/sway/config`, Sway socket discovery, the Sway boot-attempt
guard, portal environment repair, output configuration, and config validation
all currently occur in multiple installer, LiveCD, package, and recovery paths.
Each path needs an explicit Wayfire equivalent and a Sway fallback until the
Wayfire path passes installation testing.

## Shell ownership and themes

PosterChanUI remains the only desktop shell.  Set Wayfire's wf-shell autostart
off: it must not add a second panel, dock, launcher, notification area, or
background over PosterChan's taskbar/start UI.

Do not enable two decoration providers.  Wayfire's built-in `decoration` plugin
and an external GTK decoration plugin at the same time produce duplicate
frames.  The stable Gentoo Wayfire is 0.10.1, while the current gtkdecor plugin
requires Wayfire 0.11, so gtkdecor is not part of the initial image.

PosterChan-owned shell, popup, start, tray, notification, profile, and framed
application surfaces must be excluded from server-side decorations.  Ordinary
native Firefox, Telegram, Steam-client, terminal, and LibreOffice windows must
receive exactly one draggable/resizable compositor frame.  Fullscreen games
must receive none.  The macOS-style and Windows-11-style PosterChan themes own
their control order and chrome; compositor decoration settings must never
reorder or duplicate those controls.

Shadows and animations must be compositor effects only around true native
toplevels.  PosterChanUI owns shadows for its DOM chrome and popups.  Wayfire's
blur, wobbly, cube, fire, and similar effects stay disabled by default: they add
GPU work, make geometry/focus tests nondeterministic, and are inappropriate for
a gaming session.  Snap previews and snap layouts remain PosterChanUI features;
the Wayfire grid plugin may execute the final geometry but must not draw a
second overlay.

## Acceptance matrix

Run every row in both macOS-style and Windows-11-style themes, at scale 1.0,
1.25/1.5, and 2.0, on one output and mixed-scale dual outputs:

| Area | Required result |
| --- | --- |
| Session | fresh boot, recovery launch, shell update, and logout return a usable desktop with one shell per output |
| Focus | clicking every PosterChan and native window focuses and raises exactly that toplevel; delayed compositor events cannot steal focus |
| Chrome | exactly one title/control set; correct macOS/Windows button order; drag, resize, minimize, maximize, close, and back work |
| Shell UI | taskbar/dock, Start, tray, notifications, connectivity, and snap preview never appear behind apps or flash at screen center |
| Launch | Firefox, Telegram, Terminal, Office, Steam, and generic desktop entries launch exactly once on the active output |
| Gaming | Steam and Proton launch; Gamescope and direct fallback both work; fullscreen, pointer lock, controller, overlay, Alt-Tab, HDR/VRR where supported, and resolution changes work |
| Outputs | hotplug, rotation, mixed scaling, moving windows, suspend/resume, and unplugging the focused output preserve ownership and usable geometry |
| Portals | file picker, screen/window sharing, OBS recording, screenshots, clipboard, drag/drop, and notifications work |
| Remote | SPICE/VNC-style resize and absolute pointer coordinates remain aligned after every resolution/scale change |

The automated gate needs a headless compositor test for IPC operations and
event ordering, static package/config tests, and the existing desktop focus and
exact-one-launch suites against both backends.  Release testing then requires a
real-GPU desktop session, a Gamescope/Proton game, a clean LiveCD boot, a clean
install, and reboot with the installation medium removed.

## Rollout

Install Wayfire and its adapter alongside Sway first.  Select it only through an
explicit diagnostic session flag.  Preserve a boot-menu or TTY recovery path
that starts the known-good Sway session.  Promote Wayfire to the default only
after the complete matrix passes on the installed desktop and LiveCD; remove
Sway only in a later release after rollback telemetry is no longer needed.
