# PosterChanOS: a desktop computer that signs in with Nostr

Most Nostr clients live inside an operating system that uses a completely different identity. You
sign into Nostr with a key, sign into the computer with a local username, and keep the browser
between the two.

**PosterChanOS makes the Nostr identity the desktop identity.** It is an encrypted, Gentoo-based
Linux system that boots directly into the PosterChan desktop. Social, Messages, Notes, Files, Music,
Signer, Terminal and AI are applications on the desktop, while Firefox, Telegram, Steam and other
native Linux programs run beside them.

## The shell and the operating system

Sway owns the hardware-facing layer: displays, input, native Wayland windows, idle behavior and
recovery keys. The PosterChan desktop process owns what the user sees: wallpaper, launcher, taskbar,
widgets, power controls and application windows.

PosterChan applications share one client, one local store and one set of relay connections instead
of opening an iframe and another socket for every window. Native applications remain real processes
managed by the compositor. PosterChan places and decorates them so both kinds of application belong
to one desktop without pretending Firefox is a web app inside another web app.

If the desktop renderer needs to restart, the compositor and native applications can remain alive.
The recovery shortcut replaces the shell layer instead of throwing away the graphical session.

## An `npub` becomes a private Unix account

A profile switch is not enough on a shared computer. Files need the same isolation users expect from
a normal multi-user operating system.

At first sign-in, PosterChanOS validates the complete bech32 `npub`, hashes it into a stable Linux
account name, and provisions a home owned only by that account. Home directories are mode `0700`, so
another Nostr identity on the computer cannot browse the first identity's downloads, keys or app
state. The mapping hashes the full public key rather than truncating it, preventing two similar keys
from landing in the same account.

The first identity to claim a fresh installation becomes its administrator. The claim is atomic;
later identities are ordinary users. Root is not the daily desktop account. The rule is simple: the
first person in owns the machine, and everyone after that receives a separate private workspace.

## Three layers protect three different things

The installer creates a LUKS-encrypted root on Btrfs and generates the initramfs and bootloader for
that exact volume. Disk encryption protects a powered-off laptop. Unix permissions isolate people
using the same running laptop. Nostr and Blossom handle portable data:

- settings and application documents are signed, encrypted Nostr events;
- private file bytes are encrypted before upload to Blossom;
- the home directory holds a device's working data and caches;
- relays and media servers can synchronize ciphertext without reading it.

None of these is a substitute for the others. Together they cover the physical disk, the local
account boundary and synchronization between devices.

## SMS and MMS over Nostr

PosterChan's Android app can bridge a cellular subscription to every PosterChan client. The phone's
Android message provider remains authoritative and the phone is the only endpoint that uses its
radio. Nostr provides the encrypted synchronization and control plane.

Messages are represented by encrypted, addressable records. Bodies go into the encrypted
**Messages** area of the user's Blossom drive. MMS originals and small previews go into the
encrypted **MMS** area. A conversation downloads the thumbnail first and fetches the original only
when opened, reducing bandwidth without leaving the attachment in plaintext.

A message sent from a laptop becomes an encrypted, idempotent Nostr request. The phone consumes the
request, sends through Android's telephony stack, and replaces it with a completion marker. That
acknowledgement prevents reconnects from sending the message twice. If the phone is offline, the
client reports that the message is waiting for the phone instead of claiming it was delivered.

MMS already stored on Android can be mirrored with its attachments. Fetching a newly announced MMS
from a carrier MMSC is a separate telephony operation and is not currently supported; PosterChan
states that boundary instead of creating an empty message.

## Virtual machines without virt-manager

PosterChanOS includes QEMU and libvirt and provides its own Virtual Machines app. It creates machines
from an ISO, manages their power state and opens their graphical console without installing the full
virt-manager Python/GTK application stack. Only the small SPICE viewer is needed for the display.

VMs use libvirt's `qemu:///session` connection. Their definitions and virtual disks belong to the
current Unix identity inside its private home. One Nostr user on a shared PosterChanOS computer
therefore cannot list, start or view another user's virtual machines.

The creation screen supports Linux and Windows guests, UEFI or legacy BIOS, configurable CPU,
memory and qcow2 disk size. The Windows preset enables Secure Boot and a software TPM 2.0, while
SPICE carries the display and sound into the viewer. QEMU, libvirt, SeaBIOS, edk2, `swtpm` and the
small SPICE viewer are installed by PosterChanOS itself; the full virt-manager application and its
Python/GTK management stack are not required.

## Bluetooth belongs in the volume mixer

Audio routing is one task, so PosterChanOS does not hide Bluetooth pairing in an unrelated settings
application. **Volume Mixer → Bluetooth** powers the adapter, scans, pairs, connects, disconnects and
forgets devices. Once connected, a headset or speaker appears in the same PipeWire output/input
selectors as the laptop speakers, microphone and USB audio devices. The mixer also provides master
input/output controls and a separate volume and mute control for every playing application.

Fresh installations include BlueZ and Bluetooth-enabled PipeWire, and start the Bluetooth service
automatically. VM sound uses the same PipeWire session through SPICE, so selecting a Bluetooth
headset as the output also routes virtual-machine audio there.

## Task Manager and familiar recovery controls

PosterChanOS includes its own Task Manager instead of requiring a terminal-only process monitor. It
shows live CPU, memory and network use, lists processes, searches them and can end processes owned by
the signed-in Unix account. CPU, RAM and network are also available as desktop widgets.

Pressing **Ctrl+Alt+Delete** opens that Task Manager even when a native application or VM viewer has
focus. Sway captures the system shortcut and sends it to the PosterChan shell, which brings itself
forward after drawing the Task Manager. The separate Ctrl+Alt+Backspace recovery shortcut restarts
only the desktop renderer, leaving Sway and native applications running.

## Remote Desktop over Nostr

Remote screen sharing can be addressed by **`npub`**, by a PosterChan host/IP, or by
`name@host` when a machine advertises several identities. An IP is used for discovery rather than
trusted as an identity: the host's NIP-05 document supplies the recipient public key and relay, then Nostr provides
identity, permission records and WebRTC signaling; the encrypted screen stream travels directly
between peers when possible and through TURN only when NAT requires it. A LAN connection therefore
uses the local path automatically without asking the user to type an address.

The viewer is intended to work from PosterChan on Android, a phone browser, an ordinary browser, the
desktop app or desktop mode. Touch input maps to pointer gestures and an on-screen keyboard. A
PosterChanOS host can optionally grant whole-machine keyboard and mouse control through its native
bridge; a browser-only host can share a browser-selected screen but cannot accept system-wide input
because browsers correctly forbid that capability.

Auto-accept will be an explicit permission attached to one exact `npub`, with separate **view only**
and **full control** grants. It will never be a global switch. An active share will keep a persistent
indicator and immediate Stop control visible on the host.

## Updating and recovering

`update-posterchan` updates the desktop and the PosterChanOS session package together. The session
package owns the Sway configuration, system helpers, boot theme and current installer, so a normal
update cannot leave an old recovery script behind.

The canonical installer and live-image builder is [`os/gentoo.sh`](../../os/gentoo.sh). Keeping it in
the repository and shipping it through the Gentoo overlay makes installation and recovery part of
the product rather than a script stranded on one machine.

PosterChan still runs as a web client, PWA, desktop app and Android app. PosterChanOS is the edition
for a computer dedicated to the whole environment: one Nostr identity, one private desktop, one
encrypted disk, with the same PosterChan workspace available on every device.
