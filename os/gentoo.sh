#!/usr/bin/bash

# ===============================================================================================
# PUT THE TERMINAL BACK ON THE WAY OUT.
#
# "after leaving gentoo.sh, terminal is messed up again. adding extra characters as I type."
#
# This script drives the terminal hard and never gave any of it back. `read -e` turns on readline,
# which enables BRACKETED PASTE (`ESC[?2004h`) and application cursor keys; `clear` and the colour
# codes do their own work. Bash restores what IT set when a normal interactive shell exits — but a
# script that is quit part-way, that exits from inside a menu branch, or that is killed while a
# `read` is pending leaves those modes switched on in the terminal it was running in. What is left
# behind is a tty that echoes paste markers and duplicates what you type, which is exactly what
# "adding extra characters" is.
#
# `stty sane` restores echo, canonical mode and the control characters; the two escape sequences
# switch bracketed paste and application cursor keys off explicitly, because `stty` knows nothing
# about either — they are the EMULATOR's state, not the line discipline's.
#
# On EXIT, so it runs however the script ends: falling off the end, an `exit` from a menu branch, or
# Ctrl-C. Guarded and silenced, because this must never itself become the thing that fails — if
# there is no terminal (a pipe, a cron job) there is nothing to restore and nothing to say about it.
# ===============================================================================================
_pc_tty_restore() {
	[[ -t 0 ]] || return 0
	stty sane 2>/dev/null
	printf '\033[?2004l\033[?1l\033>' 2>/dev/null
}
trap _pc_tty_restore EXIT INT TERM

# ===============================================================================================
# CAN THIS mksquashfs ACTUALLY WRITE A ZSTD IMAGE? Asked by DOING it.
#
# "i already recompiled with zstd and now your version tried to recompile something and then says
# it rebuilt and still has no zstd."
#
# The old probe was `mksquashfs -help | grep -qw zstd`, and it is wrong on any current
# squashfs-tools: 4.6 turned `-help` into a short summary and moved the compressor list behind
# `-help-all` / `-help-comp`. So on a machine that had ALREADY been rebuilt with the flag the probe
# found nothing, rebuilt it again for no reason, ran the identical probe, found nothing again, and
# announced that the rebuild had failed. Every part of that was the probe.
#
# Parsing help text is guessing at an interface that is allowed to change. Compressing one file is
# not: it either produces an image or it does not, on every version there has ever been, in a few
# milliseconds. Anything printed goes to the log rather than the screen — a probe is not news.
# ===============================================================================================
_pc_mksquashfs_zstd() {
	command -v mksquashfs >/dev/null 2>&1 || return 1
	local T O rc
	T="$(mktemp -d 2>/dev/null)" || return 1
	O="$(mktemp -u 2>/dev/null)" || { rm -rf "$T"; return 1; }
	echo probe >"$T/probe" 2>/dev/null
	mksquashfs "$T" "$O" -comp zstd -no-progress -quiet >>"${LOG:-/dev/null}" 2>&1
	rc=$?
	rm -rf "$T" "$O" 2>/dev/null
	return $rc
}

########################
# What this script is:
#
# An automatic installer for Gentoo Stable with the following features:
# 1. KDE with SystemD
# 2. Full Disk Encryption
# 3. Automatic BTRFS Snapshots at Boot
# 4. The ability to build a custom and deployable image onto any machine
# 5. Easily create a bootable USB drive
# 6. Automatic Partitioning
# 7. Ability to backup or restore OS to and from a remote machine via SSH
#
# INSTRUCTIONS
#
# "Install this Live image" prepares and verifies the selected disk itself. The separate Initialize
# option remains a repair/advanced tool; a normal install must not depend on running it first.
#
# Before running the install, ensure that you have Internet access.
#
# Please be sure to change USER,USER_PASSWORD, DISK_PASSWORD, and ROOT_PASSWORD strings in this file
#
# To install PosterChanOS from the live image, run gentoo.sh and choose option 9.
#
########################
#Configure this section
########################
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Cyberpunk color codes
# WHERE THIS SCRIPT'S SUPPORT FILES ARE — asked, not assumed from $0.
#
# gentoo.sh needs `bin/` (the pc-* helpers it installs) and `plymouth/` (the boot theme) beside it,
# and it used to find them with `$(dirname "$0")`. That is right when it is run out of a checkout and
# WRONG the moment it is installed: at /usr/bin/gentoo.sh, dirname is /usr/bin, so it looks for
# /usr/bin/bin and /usr/bin/plymouth, finds neither, and carries on. Nothing fails — the helpers are
# simply not copied, and the first sign is a desktop with no pc-shell-start on the machine it just
# installed.
#
# So the directory is resolved once, from the places the tree actually lives: beside the script, then
# where the LiveCD builder puts it, then the two staging paths the installer already used.
PCOS_TREE=""
for _d in "$(cd "$(dirname "$0")" 2>/dev/null && pwd)" /usr/local/share/posterchanos \
          /usr/share/posterchan /tmp; do
	[ -n "$_d" ] && [ -d "$_d/bin" ] && { PCOS_TREE="$_d"; break; }
done
# Nothing found is not fatal here: every use site already has its own fallbacks, and an install from
# a bare script is still better than no install. It just cannot copy what it does not have.
[ -n "$PCOS_TREE" ] || PCOS_TREE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"

COLOR_RED="\033[1;31m"; COLOR_CYAN="\033[1;36m"; COLOR_MAGENTA="\033[1;35m"; COLOR_YELLOW="\033[1;33m"
COLOR_GREEN="\033[1;32m"; COLOR_RESET="\033[0m"; COLOR_BOLD="\033[1;97m"
TARGET='/tmp/install'
mkdir $TARGET
######################################
echo
HARD_DISK=$2
######################################
USER="verita84"
USER_PASSWORD="123456"
ROOT_PASSWORD="123456"
WIRELESS_PASSWORD='123456'
SSID='123456'
WIRELESS_INTERFACE='wlan0'
COMPRESSION='compress=zstd:10'
#Full Disk Encryption Settings
AUTO_DECRYPT='True'
DISK_PASSWORD='123456'
##############################
REPO_CHOICE="local"
#Overrided Swap File Size
#SWAP_SIZE='1G'
#
# OpenSSH stays installed for recovery/admin use, but a fresh desktop must not expose a password
# daemon before its administrator deliberately enables it (`systemctl enable --now sshd`).
SERVICES+=(systemd-timesyncd libvirtd bluetooth smartd cups NetworkManager)
MAKEOPTS="-j$(cat /proc/cpuinfo | grep -i processor | grep -vi 'model' | wc -l)"
ROOT_PARTITION_SIZE="30GB"
FEATURES="-pid-sandbox getbinpkg -binpkg-request-signature"
EMERGE_DEFAULT_OPTS="--jobs 5 --getbinpkg "
#USEFLAG CONFIGURATION
# zstd is GLOBAL, not per-package: the live CD build compresses its squashfs with it and
# dracut needs to be able to read that back, so a kernel/initramfs built without the flag
# fails at "zstd is not supported" — after the whole image has been built.
USE_FLAGS=" flatpak dracut -webp -ladspa -gpm npm introspection lame systemd-boot dist-kernel luks cryptsetup kernel-install boot opus theora vpx kernel-install systemd firmware btrfs networkmanager zstd opengl vulkan"
# Physical GPUs plus VirGL, which is the accelerated virtio-gpu path used by QEMU/KVM. Without
# virgl Mesa prints "virtio_gpu: driver missing" on the first installed boot: Sway starts, but EGL
# cannot render and the VM remains a black screen even though the same live medium appeared fine.
VIDEO_CARDS="intel amdgpu radeon radeonsi virgl"
#
#PACKAGE CONFIGURATION
BASE_PACKAGES="net-print/cups-filters net-misc/networkmanager net-wireless/bluez net-fs/sshfs app-shells/starship dev-util/sh sys-boot/plymouth sys-power/acpid app-arch/zip dev-python/virtualenv sys-apps/flatpak sys-power/powertop app-shells/bash-completion sys-power/cpupower media-libs/gexiv2 media-plugins/gst-plugins-pulse mail-mta/postfix app-admin/sysstat sys-apps/smartmontools net-fs/nfs-utils net-firewall/nftables dev-python/pip sys-fs/inotify-tools net-analyzer/nmap app-misc/screen app-portage/gentoolkit sys-fs/dosfstools app-admin/sudo sys-apps/systemd sys-apps/util-linux app-eselect/eselect-repository dev-vcs/git sys-block/parted sys-process/btop net-vpn/wireguard-tools app-editors/neovim app-misc/fastfetch sys-fs/btrfs-progs net-print/cups sys-firmware/seabios-bin sys-firmware/edk2-bin app-emulation/libvirt app-emulation/qemu app-emulation/virt-viewer app-emulation/spice-vdagent app-crypt/swtpm"
SPECIAL_PACKAGE_USE=("kde-apps/kio-extras samba mtp" "app-db/postgresql icu lz4 nls pam readline server ssl system zlib zstd uuid" "dev-build/meson test test-full" "dev-qt/qtwebengine bindist" "media-sound/sox -opus" "media-video/vlc -opus -theora -vpx" "dev-qt/qtpositioning geoclue" "media-libs/libvpx postproc" "dev-python/pillow webp" "gui-libs/gtk colord sysprof" "media-libs/freetype harfbuzz" "dev-lang/php gmp sodium sysvipc calendar bcmath exif bzip2 intl ctype curl fileinfo filter gd iconv ssl posix session simplexml xmlreader xmlwriter zip zlib postgres png opcache jit cli fpm zip pdo" "net-im/synapse postgres" "net-p2p/qbittorrent webui" "app-crypt/certbot certbot-nginx" "acct-user/git gitea" "app-admin/vaultwarden web postgres" "media-gfx/imagemagick -postscript" "media-gfx/imagemagick -postscript dev-libs/jemalloc statsv" "media-libs/libsdl2 -kms -pipewire" "media-video/obs-studio pipewire wayland" "media-video/pipewire sound-server bluetooth" "gui-wm/sway X" "x11-libs/libXrandr abi_x86_32" "mail-mta/postfix sasl" "app-emulation/qemu spice usbredir pipewire virgl" "app-emulation/libvirt qemu virt-network" "app-emulation/virt-viewer spice")
#
# External desktop monitors expose brightness over DDC/CI rather than /sys/class/backlight.
BASE_PACKAGES="www-client/firefox-bin $BASE_PACKAGES"
# ── PosterChanOS ────────────────────────────────────────────────────────────────────────────────
# The shell is the PosterChan desktop itself, so there is no second desktop environment to install.
# A browser and a Steam game have to appear on that desktop, and those two rule out every embedding
# trick between them: a browser could be reparented into our window, but a GAME cannot — reparenting
# costs the direct-rendering path, Vulkan surfaces do not survive it, and a screencast adds a copy
# per frame to the one workload that cannot afford one. So the arrangement is the ordinary one: a
# compositor owns the screen, both are ordinary clients, and PosterChan decides where they go. They
# are "inside PosterChan" because PosterChan IS the desktop.
#
# sway rather than a compositor of our own: wlroots-based, mature, and it ships XWayland, which is
# how Steam and most games get on screen at all. PosterChan drives it over its JSON IPC (desktop/wm.js).
#
# What is deliberately NOT here: plasma-meta and every kde-app, virt-manager, obs, kdenlive, vscodium,
# telegram, elisa, discover — and flatpak, whose only real customer was Steam, which portage builds
# natively. That is most of a Plasma desktop's disk and nearly all of its build time.
# SCREEN CAPTURE IS THE PART THAT NEEDS PLANNING, not the part that comes for free. On X11 anything
# could read the screen, which is why it worked without anyone thinking about it; Wayland has no such
# call by design, so a recorder gets frames through the ScreenCast PORTAL over PipeWire. That is
# three packages that must all be present and agree — the portal front end, the wlroots BACK end
# (there is no generic one; the front end alone answers "no such capture" and OBS shows a screen
# capture source that lists nothing), and OBS built with the pipewire USE flag. Missing any one of
# them fails at the moment somebody presses record, which is the worst possible moment to find out.
#
# AND A FOURTH, for a portal a recorder never touches: the wlroots backend implements ScreenCast and
# NOTHING ELSE, so with it alone there is no FileChooser interface on the bus at all. Measured: the
# desktop app's own log carries `No such interface "org.freedesktop.portal.FileChooser"` and a
# failure to read the portal version, which is Folder Sync's "choose a folder" having nowhere to
# ask. `xdg-desktop-portal-gtk` is the backend that answers it — GTK, which sway's own stack pulls
# in regardless, and emphatically not webkit.
#
# SCREENSHOTS are `gui-apps/grim` (+ `gui-apps/slurp` to choose an area). grim reads the
# compositor's own output, so it captures everything on the screen INCLUDING the native app
# surfaces a PosterChan window holds over a hole in the page — which Electron's own capturePage()
# renders as a black rectangle, and which the portal screencast path only reaches by putting a
# "share your screen?" dialog in front of every single screenshot. Without these two the tray hides
# its Screenshot tile and PrtSc says which package is missing, which is honest and is not the same
# thing as working.
# EVERY PROGRAM THE SHELL SHELLS OUT TO IS NAMED HERE, and each line says which bridge needs it.
#
# The shell is an Electron page; everything it knows about this machine it learns by running a
# command. When one of those is missing the bridge does not crash — it returns a refusal, and a
# refusal that nobody installed a package for reads to the person using it as a control that does
# nothing. `grim` was the live example: it is the entire screenshot feature, it was in no list, and
# the tray could only ever have apologised for it.
#
# `brightnessctl` is the counter-example and is why this comment exists rather than a longer list:
# desktop/power.js falls back to it when /sys/class/backlight is root-owned, and it is NOT IN THE
# GENTOO TREE — adding it breaks emerge on every fresh build. It is deliberately absent, and what
# makes the slider work instead is the udev rule further down that gives the backlight to the
# `video` group, which pc-provision-user puts every account in. Do not "fix" a brightness slider by
# adding that package; check the rule and the group.
#
# So this list is derived from the CODE, not from what happened to be installed on the test laptop —
# where `xdg-open` and `nmcli` were both present as somebody else's dependency, which is exactly how
# a tool goes missing on the next fresh build with nothing to say why.
# Audited against desktop/*.js: grim slurp wl-copy wpctl nmcli systemctl xdg-open script sudo
# swaymsg (+ brightnessctl, see above). `tests/test_posterchanos_profile.py` re-runs that audit.
POSTERCHANOS_PACKAGES="gui-wm/sway gui-apps/swaybg x11-base/xwayland gui-apps/foot app-misc/ddcutil \
gui-apps/wl-clipboard \
gui-apps/grim gui-apps/slurp \
x11-misc/xdg-utils \
media-video/pipewire media-video/wireplumber gui-libs/gtk media-fonts/noto media-fonts/noto-emoji \
www-client/firefox-bin \
games-util/steam-launcher gui-wm/gamescope games-util/game-device-udev-rules \
media-libs/mesa media-libs/vulkan-loader dev-util/vulkan-tools \
sys-apps/xdg-desktop-portal gui-libs/xdg-desktop-portal-wlr sys-apps/xdg-desktop-portal-gtk \
media-video/obs-studio \
sec-keys/openpgp-keys-gentoo-release dev-vcs/git \
net-vpn/tor gui-apps/swayidle"
# net-misc/networkmanager (nmcli, the whole network tray), app-admin/sudo, sys-apps/systemd
# (systemctl: sleep, reboot, power profiles) and sys-apps/util-linux (`script`, which IS the local
# terminal's PTY — see desktop/localterm.js) come from BASE_PACKAGES / @system above. They are
# listed here in a comment rather than repeated as packages so the audit above stays complete
# without emerge being asked for the same thing twice.

# THIS SCRIPT BUILDS POSTERCHANOS. THERE IS NO SECOND PROFILE.
#
# It used to build two — PosterChanOS, and a KDE Plasma desktop (`kde-plasma/plasma-meta` plus
# dolphin, konsole, kdenlive, kcalc, discover and a flatpak app list) — chosen by a variable that
# EIGHT separate places branched on. Every one of those was a chance for the two halves to disagree,
# and they did: a chroot does not inherit an environment, so the choice had to become a FILE after a
# chroot run silently rebuilt the KDE list and installed an entire second desktop on the profile
# whose whole point is not having one.
#
# That is fragmentation with nothing on the other side of it. `/etc/posterchanos` is still written,
# because an installed system should be able to say what it is, but nothing branches on it.
PACKAGES="$BASE_PACKAGES $POSTERCHANOS_PACKAGES"
TMPFS_SIZE="32G"
CPU_TYPE="x86-64-v3"
BUILD_SERVER="n"
BUILD_SERVER_ADDRESS="nas.lan"
BUILD_PATH="/raid/gentoo-desktop.lan"
RSYNC_EXCLUDES=" --exclude=-/var/lib/containers --exclude=/var/lib/containerd --exclude=/var/lib/docker --exclude=/var/lib/flatpak --exclude=/home --exclude=/var/lib/pleroma/uploads --exclude=/var/lib/distfiles --exclude=/var/lib/owncloud --exclude=/etc/disk --exclude=/etc/mtab --exclude=/swap --exclude=@swap --exclude=/mnt --exclude=/snapshots --exclude=/backup --exclude=/raid --exclude=/var/tmp/* --exclude=/tmp/* --exclude=/var/lib/libvirt/* --exclude=/var/cache --exclude=/var/notmpfs --exclude=/var/lib/systemd/coredump/* --exclude=/var/cache/* --exclude=/.snapshots/* --exclude=/sys/* --exclude=/dev/* --exclude=/proc/* --exclude=/run/*"
#Add Masked Packages to the Array
MASKED_PACKAGES+=(www-apps/jellyfin-bin app-admin/vaultwarden dev-util/nvidia-cuda-toolkit www-apps/radicale www-apps/vaultwarden-web www-apps/radicale net-misc/owncloud-client net-libs/libre-graph-api-cpp-qt-client media-video/obs-studio net-misc/sunshine dev-util/sh net-misc/moonlight app-admin/bitwarden-desktop-bin net-im/element-desktop-bin net-misc/nyx net-libs/stem sys-libs/libudev-compat dev-libs/nss dev-libs/libappindicator media-video/ffmpeg games-util/game-device-udev-rules games-util/steam-launcher net-im/telegram-desktop-bin)

fixSound() {
	/usr/bin/systemctl --user disable --now pulseaudio.socket pulseaudio.service
	/usr/bin/systemctl --user enable --now pipewire-pulse.socket wireplumber.service
	/usr/bin/systemctl --user enable --now pipewire.service
}

gentooRepo() {
	echo
	clear
	mkdir -p $TARGET/etc/portage/repos.conf/
	echo -e "\033[1;36m◆ CONFIGURING REPOS ◆\033[0m"

	# `emerge --sync` MUST WORK ON A MACHINE THAT IS NOT ON THIS LAN — EVERY MACHINE, NOT JUST THE OS.
	#
	# This used to write rsync://gentoo-repo.lan, which resolves on exactly one network. Anywhere
	# else that is a broken --sync from first boot, and the way somebody finds out is that their
	# machine can never update — no error at install time, nothing in any log, just a computer that
	# quietly stops being able to receive a fix.
	#
	# The PosterChanOS arm was moved to webrsync first and the OTHER arm was left on the .lan name,
	# on the reasoning that the plain-server profile is "somebody's own machines on their own
	# network". That reasoning is wrong the moment anybody else installs a server, which is the
	# entire point of shipping an installer — and the two arms fail in the same silent way. There is
	# one URI now and it works from anywhere, including from this LAN.
	#
	# webrsync fetches a SIGNED SNAPSHOT TARBALL over HTTPS. PosterChan's endpoint caches Gentoo's
	# signed snapshots and release packages. Clients still
	# verify the upstream signature, while repeated installs and updates stay on our fast web cache.
	{
		echo "[gentoo]"
		echo "location = /var/db/repos/gentoo"
		echo "sync-type = webrsync"
		echo "sync-uri = https://gentoo.poster.place"
		echo "sync-webrsync-verify-signature = true"
	} >$TARGET/etc/portage/repos.conf/gentoo-mirror.conf

	# THE POSTERCHANOS OVERLAY: how an installed machine gets a newer desktop and session without
	# being reinstalled. A git repo rather than a directory of files, because that is the only shape
	# portage can sync over plain https.
	{
		echo "[posterchan]"
		echo "location = /var/db/repos/posterchan"
		echo "sync-type = git"
		echo "sync-uri = https://gentoo.poster.place/posterchan-overlay.git"
		echo "auto-sync = yes"
		echo "priority = 100"
		# BOTH depths, and they are different options. `clone-depth` governs the FIRST clone
		# and `sync-depth` the updates after it — setting only the second leaves portage
		# running `git clone --depth 1`, which a dumb HTTP transport cannot do: "fatal: dumb
		# http transport does not support shallow capabilities". The repo directory is then
		# left empty and every emerge behaves as though the overlay has no packages in it,
		# with the failure buried in a sync log nobody reads. (Read out of portage's own
		# git.py rather than guessed — the names are not symmetrical.) The overlay is a few
		# hundred kilobytes; full clones cost nothing here.
		echo "clone-depth = 0"
		echo "sync-depth = 0"
	} >$TARGET/etc/portage/repos.conf/posterchan.conf

	mkdir -p $TARGET/etc/portage/binrepos.conf
	echo "[binhost]" >$TARGET/etc/portage/binrepos.conf/gentoobinhost.conf
	echo "priority = 9999" >>$TARGET/etc/portage/binrepos.conf/gentoobinhost.conf
	echo "sync-type = webrsync" >>$TARGET/etc/portage/binrepos.conf/gentoobinhost.conf
	echo "sync-uri = https://gentoo.poster.place/releases/amd64/binpackages/23.0/x86-64/" >>$TARGET/etc/portage/binrepos.conf/gentoobinhost.conf

	# https, not http: this is fetched by machines that are not on a trusted network, and a plain
	# http mirror is one anybody in the path can rewrite.
	# This function is also the installed-system repair command (`gentoo.sh repo`). Re-running it
	# must replace the setting rather than accumulating a new GENTOO_MIRRORS assignment every time.
	sed -i '/^[[:space:]]*GENTOO_MIRRORS=/d' "$TARGET/etc/portage/make.conf"
	echo "GENTOO_MIRRORS=\"https://gentoo.poster.place\"" >>"$TARGET/etc/portage/make.conf"
}

partitionDetection() {
	clear
	if [ -f "/etc/disk" ]; then
		echo -e "\033[1;33mReading from /etc/disk\033[0m"
		HARD_DISK=$(cat /etc/disk | head -1)
		ROOT_NAME=$(cat /etc/disk | tail -2 | head -1)
	fi

	if [ -f "/tmp/disk" ]; then
		echo -e "\033[1;33mReading from /tmp/disk\033[0m"
		HARD_DISK=$(cat /tmp/disk | head -1)
		ROOT_NAME=$(cat /tmp/disk | tail -2 | head -1)
	fi

	# Children of THIS disk only. Grepping all blkid output matched serials, mapper UUIDs and names
	# containing the same text; one corrupt prompt then assembled a mapper name from three devices.
	local DISK_PATH="/dev/$HARD_DISK"
	# A newly-created ESP has no filesystem yet. Detecting it only by FSTYPE=vfat made EFI empty
	# between `parted mkpart` and `mkfs.vfat`; every later boot file then landed in the encrypted
	# root's /boot directory while the real ESP stayed completely blank. Prefer the GPT ESP type,
	# retain vfat compatibility for existing disks, then fall back to this layout's first partition.
	EFI="$(lsblk -nrpo NAME,TYPE,FSTYPE,PARTTYPE "$DISK_PATH" 2>/dev/null \
		| awk '$2=="part" && (tolower($4)=="c12a7328-f81f-11d2-ba4b-00a0c93ec93b" || $3=="vfat") {print $1; exit}')"
	[ -n "$EFI" ] || EFI="$(lsblk -nrpo NAME,TYPE "$DISK_PATH" 2>/dev/null \
		| awk '$2=="part" {print $1; exit}')"
	BTRFS="$(lsblk -nrpo NAME,TYPE "$DISK_PATH" 2>/dev/null \
		| awk '$2=="part" {n++; if(n==2){print $1; exit}}')"
	local LUKS_UUID=""
	[ -n "$BTRFS" ] && LUKS_UUID="$(/sbin/blkid -s UUID -o value "$BTRFS" 2>/dev/null)"
	ROOT_MAPPER_NAME="/dev/mapper/luks-$LUKS_UUID"

	echo
	echo
	echo -e "\033[1;33m◆ DEVICE DETECTION ◆\033[0m"
	echo
	echo -e "\033[1;33mHard Disk: $HARD_DISK\033[0m"
	echo -e "\033[1;33mBTRFS Mapper Name: $ROOT_MAPPER_NAME\033[0m"
	echo -e "\033[1;33mBTRFS Encrypted Volume: $BTRFS\033[0m"
	echo -e "\033[1;33mBTRFS Subvolume: $ROOT_NAME\033[0m"
	echo -e "\033[1;35m--------------------------------------------\033[0m"
	echo
}

partitionDetection

decryptBoot() {
	KEYFILE='keyfile.key'
	echo
	echo -e "\033[1;33m◆ SETTING LUKS KEYFILE ◆\033[0m"

	echo
	echo -e "\033[1;33mClearing Old Keys\033[0m"
	echo
	for i in 1 2 3 4 5 6; do
		printf "$DISK_PASSWORD" | cryptsetup luksKillSlot $1 $i
	done
	dd if=/dev/urandom of=/boot/$KEYFILE bs=1024 count=4 || return 1
	chown root:root /boot/$KEYFILE
	chmod 0400 /boot/$KEYFILE
	echo
	echo -e "\033[1;33mAdding new key......\033[0m"
	echo
	printf '%s' "$DISK_PASSWORD" | cryptsetup luksAddKey "$1" /boot/$KEYFILE || return 1
	echo "install_items+=\" /boot/unlock.sh /boot/$KEYFILE \"" >>/etc/dracut.conf
	echo "omit_drivers+=\" nouveau \"" >>/etc/dracut.conf

	sed -i "s/none/\/boot\/$KEYFILE/" /etc/crypttab
	echo "#!/bin/bash" >/boot/unlock.sh
	echo "systemd-cryptsetup attach $(echo $ROOT_MAPPER_NAME | grep luks | cut -d '/' -f4)  UUID=$(/sbin/blkid -s UUID -o value ${BTRFS}) /boot/$KEYFILE " >>/boot/unlock.sh
	chmod +x /boot/unlock.sh
	return 0
}

autoLogin() {
	echo -e "\033[1;33mRemoved for now\033[0m"
	#GETTY_DIR="$TARGET/etc/systemd/system/getty@tty1.service.d"
	#GETTY="$GETTY_DIR/override.conf"
	#mkdir -p $GETTY_DIR
	#echo "[Service]" >$GETTY
	#echo "ExecStart=" >>$GETTY
	#echo "ExecStart=-/sbin/agetty --autologin $USER --noclear %I /usr/bin/bash" >>$GETTY
}

systemMounts() {
	echo
	echo -e "\033[1;32m◆ CHECKING FOR BTRFS PARTITION ◆\033[0m"

	if [[ -e "$BTRFS" ]]; then
		partitions || return 1
		if [ -z "$EFI" ] || [ ! -b "$EFI" ]; then
			echo -e "\033[1;31mNo EFI System Partition was found on $DISK_PATH.\033[0m"
			return 1
		fi
		echo -e "\033[1;33mBTRFS device found\033[0m"
		echo
		echo -e "\033[1;33mMounting Boot,EFI,HOME\033[0m"
		echo
		if [ "$(blkid -s TYPE -o value "$EFI" 2>/dev/null)" != "vfat" ]; then
			echo -e "\033[1;31m$EFI is not a FAT32 EFI filesystem; refusing to install into RAM.\033[0m"
			return 1
		fi
		# A cancelled/failed attempt may have left some of the target tree mounted.  A retry must
		# start from a known state rather than failing with an unhelpful "already mounted" error.
		# Unmount children first; never use a lazy unmount here because copying into a detached tree
		# would make an apparently successful but unbootable installation.
		local old_mount
		while read -r old_mount; do
			[ -n "$old_mount" ] && umount "$old_mount" || true
		done < <(findmnt -Rrn -o TARGET "$TARGET" 2>/dev/null | sort -r)
		mkdir -p "$TARGET"
		if ! mount "$ROOT_MAPPER_NAME" "$TARGET"; then
			echo -e "\033[1;31mCould not mount encrypted root $ROOT_MAPPER_NAME at $TARGET.\033[0m"
			return 1
		fi
		mountpoint -q "$TARGET" || return 1
		btrfs_filesytem || return 1
		mkdir -p "$TARGET/boot/EFI"
		if ! mount -t vfat "$EFI" "$TARGET/boot" || ! mountpoint -q "$TARGET/boot"; then
			echo -e "\033[1;31mCould not mount the EFI System Partition at $TARGET/boot.\033[0m"
			findmnt "$EFI" 2>/dev/null || true
			return 1
		fi
		mkdir -p $TARGET/swap
		#CONFIGURE DATA DIRS (HOME)
		mkdir $TARGET/home
		mkdir $TARGET/.snapshots
		mount -o subvol=@home "$ROOT_MAPPER_NAME" "$TARGET/home" || return 1
		mount -o subvol=@swap "$ROOT_MAPPER_NAME" "$TARGET/swap" || return 1
		mkdir $TARGET/home/$USER

		mkdir $TARGET/run
		mkdir $TARGET/dev
		mkdir $TARGET/proc
		mkdir $TARGET/sys
		mkdir -p $TARGET/var/tmp/portage

		mount --types proc /proc $TARGET/proc
		mount --rbind /sys $TARGET/sys
		mount --make-rslave $TARGET/sys
		mount --rbind /dev $TARGET/dev
		mount --make-rslave $TARGET/dev
		mount --bind /run $TARGET/run
		mount --make-slave $TARGET/run
		mount -t efivarfs none $TARGET/sys/firmware/efi/efivars
		mount -t tmpfs -o size=$TMPFS_SIZE tmpfs $TARGET/var/tmp/portage
	else
		echo
		echo -e "\033[1;33mSystem Mounts: Aborting Install, $BTRFS not found!\033[0m"
		echo
		echo
		exit 1
	fi
}

unmaskPackages() {

	mkdir -p /etc/portage/package.use
	for i in "${SPECIAL_PACKAGE_USE[@]}"; do
		NAME=$(echo $i | cut -d ' ' -f1)
		FILE_NAME=$(echo $NAME | cut -d '/' -f2 | cut -d ' ' -f1)
		ARGS=$(echo $i | cut -d ' ' -f2-50)
		echo "$NAME $ARGS"> /etc/portage/package.use/$FILE_NAME
	done

	for i in "${MASKED_PACKAGES[@]}"; do
		echo "$i ~amd64" >>/etc/portage/package.accept_keywords
	done

}

updateOS() {
    /usr/bin/emerge --sync
    #/usr/bin/emerge -uDN @world --autounmask-write
	#/usr/sbin/etc-update -q --automode -5
	/usr/bin/emerge -uDN @world
    /usr/bin/emerge -c
    bootloader
}

configurePortage() {
	echo "COMMON_FLAGS=\"-march=$CPU_TYPE -O2 -pipe\"" >$TARGET/etc/portage/make.conf
	echo 'CFLAGS="${COMMON_FLAGS}"' >>$TARGET/etc/portage/make.conf
	echo 'CXXFLAGS="${COMMON_FLAGS}"' >>$TARGET/etc/portage/make.conf
	echo 'FCFLAGS="${COMMON_FLAGS}"' >>$TARGET/etc/portage/make.conf
	echo 'FFLAGS="${COMMON_FLAGS}"' >>$TARGET/etc/portage/make.conf
	echo "LC_MESSAGES=C.utf8" >>$TARGET/etc/portage/make.conf

	echo 'ACCEPT_KEYWORDS="amd64"' >>$TARGET/etc/portage/make.conf
	echo "FEATURES=\"$FEATURES\"" >>$TARGET/etc/portage/make.conf
	echo "EMERGE_DEFAULT_OPTS=\"$EMERGE_DEFAULT_OPTS\"" >>$TARGET/etc/portage/make.conf
	echo "L10N=\"en en-US\"" >>$TARGET/etc/portage/make.conf
	mkdir -p $TARGET/var/tmp/portage
	mkdir -p $TARGET/etc/portage/env

	echo 'EXTRA_ECONF="--disable-bootstrap"' >$TARGET/etc/portage/env/gcc.conf
	echo 'PORTAGE_TMPDIR="/var/notmpfs"' >$TARGET/etc/portage/env/notmpfs.conf
	echo "sys-devel/gcc gcc.conf" >$TARGET/etc/portage/package.env
	echo "sys-devel/llvm gcc.conf" >>$TARGET/etc/portage/package.env

	clear

	if [[ $REPO_CHOICE = *local* ]]; then
		gentooRepo
	fi

	chroot $TARGET /usr/bin/emerge --sync

	echo "USE=\"$USE_FLAGS\"" >>$TARGET/etc/portage/make.conf
	# Steam and Proton are 32-bit applications even on amd64. Keep both ABIs enabled and let
	# Portage select the matching current graphics/audio dependency set.
	echo 'ABI_X86="64 32"' >>$TARGET/etc/portage/make.conf

	echo "MAKEOPTS=\"$MAKEOPTS\"" >>$TARGET/etc/portage/make.conf

	echo
	echo
	echo
	echo -e "\033[1;36m◆ CONFIGURING PROFILES ◆\033[0m"
	echo
	echo
	echo
	# THE GENTOO PROFILE IS THE BIGGEST LEVER THERE IS, and it is chosen here — before a single
	# package list is consulted. The desktop/plasma profile turns on the KDE USE flags system-wide
	# and pulls Plasma into @world no matter what PACKAGES says, which is how a "minimal" build was
	# caught emerging kde-frameworks/breeze-icons. PosterChanOS takes the plain desktop profile: the
	# desktop USE defaults (which sway, pipewire and OBS all want) without a desktop environment.
	GENTOO_PROFILE=$(chroot $TARGET /usr/bin/eselect profile list | grep -i 'desktop' | grep -vi 'plasma\|gnome\|no-multilib' | grep systemd | grep -i stable | head -1 | cut -d '[' -f2 | cut -d ']' -f1)
	if [ -z "$GENTOO_PROFILE" ]; then
		echo -e "\033[1;31mNo stable multilib desktop/systemd Gentoo profile was found.\033[0m"
		return 1
	fi
	chroot $TARGET /usr/bin/eselect profile set $GENTOO_PROFILE

	# Steam is maintained in Gentoo's Steam overlay. Make it part of the ordinary install so the
	# machine is gaming-ready on first boot rather than requiring a hidden post-install step.
	chroot $TARGET /usr/bin/emerge -1 app-eselect/eselect-repository
	chroot $TARGET /usr/bin/eselect repository enable steam-overlay
	chroot $TARGET /usr/bin/emerge --sync steam-overlay

	mkdir -p $TARGET/etc/portage/package.license
	echo "*/*  *" >$TARGET/etc/portage/package.license/license
	echo 'games-util/steam-launcher steam' >$TARGET/etc/portage/package.license/posterchan-steam
	rm -rf $TARGET/etc/portage/package.accept_keywords
	mkdir -p $TARGET/etc/portage/package.mask
	echo "dev-lang/rust" >$TARGET/etc/portage/package.mask/rust

	echo
	echo -e "\033[1;33mConfiguring Binary Package GPG keys\033[0m"
	echo
	chroot $TARGET /usr/bin/getuto
}

buildGentoo() {

	echo -e "\033[1;92m◆ INSTALL BASE SYSTEM ◆\033[0m"
	echo
	echo

	echo
	echo
	echo
	echo -e "\033[1;36m[Building Base System]\033[0m"
	echo
	echo
	chroot $TARGET /usr/bin/emerge --update --deep --newuse @world --autounmask-write
	chroot $TARGET etc-update -q --automode -5
	chroot $TARGET /usr/bin/emerge --update --deep --newuse @world
	locale

	chroot $TARGET /usr/sbin/systemd-machine-id-setup

	echo
	echo
	echo
	echo -e "\033[1;36m[Installing Kernel]\033[0m"
	echo
	echo
	chroot $TARGET mkdir -p /etc/kernel/install.d
	chroot $TARGET touch /etc/kernel/install.d/05-check-chroot.install
	chroot $TARGET /usr/bin/emerge dracut sys-kernel/gentoo-kernel-bin sys-kernel/linux-firmware --autounmask-write
	chroot $TARGET etc-update -q --automode -5
	chroot $TARGET /usr/bin/emerge dracut sys-kernel/gentoo-kernel-bin sys-kernel/linux-firmware
	chroot $TARGET /usr/bin/eselect kernel set 1

	echo
	echo
	echo
	echo -e "\033[1;36m[Installing Packages]\033[0m"
	echo
	echo
	# THE MARKER GOES IN BEFORE THE PACKAGE STEP, NOT AFTER IT. install-packages runs INSIDE the
	# chroot, where an environment variable does not reach — so without this the chroot rebuilds the
	# default list and installs the whole KDE desktop, hours of it, on the profile whose entire point
	# is not having one. finalizeInstall writes the marker too; by then it is far too late.
	touch $TARGET/etc/posterchanos
	# Never depend on the caller's working directory. The LiveCD desktop starts this script through
	# a .desktop file, and its cwd is not the directory containing gentoo.sh; a relative copy either
	# failed outright or copied a stale unrelated file into the new OS.
	INSTALLER_SRC="$PCOS_TREE/gentoo.sh"
	[ -f "$INSTALLER_SRC" ] || INSTALLER_SRC="/usr/local/share/posterchanos/gentoo.sh"
	if [ ! -f "$INSTALLER_SRC" ]; then
		echo -e "\033[1;31mPosterChanOS installer source is missing — refusing to create an unrepairable target.\033[0m"
		return 1
	fi
	cp -f "$INSTALLER_SRC" "$TARGET/usr/bin/gentoo.sh"
	chroot $TARGET /usr/bin/bash /usr/bin/gentoo.sh install-packages
	echo
	echo
	echo -e "\033[1;36m[Configuring Accounts and post-setup tasks]\033[0m"
	echo
	echo
	finalizeInstall
}

finalizeInstall() {
	# A bootloader/initramfs failure is an INSTALL failure. setup.sh used to continue into accounts
	# and services after bootloader() returned non-zero, so the menu reported completion and the
	# first honest error appeared only after reboot at the maintenance prompt.
	sed -i '1i set -e' $TARGET/setup.sh
	echo 'bash /usr/bin/gentoo.sh bootloader' >>$TARGET/setup.sh
	echo 'bash /usr/bin/gentoo.sh accounts' >>$TARGET/setup.sh
	echo 'bash /usr/bin/gentoo.sh services' >>$TARGET/setup.sh
	# Do not carry the LiveCD operator into the installed system. On a live install USER=live, but
	# that account is deliberately removed; with `set -e`, chowning /home/live aborted finalization.
	# THE DISPLAY MANAGER IS A KDE COMPONENT AND PosterChanOS DOES NOT HAVE ONE. Enabling a unit
	# that was never installed fails the whole finalize step — and on the profile whose entire point
	# is that the shell IS the desktop, there is nothing for a login screen to launch. The shell
	# session (autologin into sway, which starts PosterChan) goes in instead.
	touch $TARGET/etc/posterchanos
	# Resolve again inside finalization: this function also runs in the target chroot as a fresh shell,
	# so the build-stage INSTALLER_SRC variable does not cross that process boundary.
	INSTALLER_SRC="$PCOS_TREE/gentoo.sh"
	[ -f "$INSTALLER_SRC" ] || INSTALLER_SRC="/usr/local/share/posterchanos/gentoo.sh"
	if [ ! -f "$INSTALLER_SRC" ]; then
		echo -e "\033[1;31mPosterChanOS installer source is missing during finalization — refusing to continue.\033[0m"
		return 1
	fi
	cp -f "$INSTALLER_SRC" "$TARGET/usr/bin/gentoo.sh"
	plymouthTheme || {
		echo -e "\033[1;31mPosterChanOS boot splash installation failed.\033[0m"
		return 1
	}
	chmod +x $TARGET/usr/bin/gentoo.sh
	chmod +x $TARGET/setup.sh
	cp -f /tmp/disk $TARGET/etc/disk
	# The fresh LUKS password lives only in this installer's shell. The bootloader runs in a new
	# process inside the target, where the historical source default would otherwise become 123456
	# again and `luksAddKey` would fail with "No key available with this passphrase". Pass it through
	# the chroot environment for this one operation; it is never written to setup.sh or the target.
	PC_INSTALL_PASSWORD="$DISK_PASSWORD" chroot "$TARGET" /setup.sh
	# accounts() creates `posterchan`; configure its graphical session only after that. Doing this
	# before accounts selected the LiveCD's `live` account and copied its autologin onto the NVMe.
	chroot $TARGET /usr/bin/bash /usr/bin/gentoo.sh posterchan-shell
	# FINALIZATION OWNS THE BOOT SESSION. posterchan-shell also writes these for upgrades and live
	# sessions, but an installed disk must not depend on which overlay/package branch that helper
	# took, nor on USER/HOME inherited through chroot. Write the two tiny boot-critical files against
	# the target explicitly after the account exists, then set ownership inside that target.
	mkdir -p "$TARGET/home/posterchan/.config/sway" \
		"$TARGET/etc/systemd/system/getty@tty1.service.d"
	[ -e "$TARGET/home/posterchan/.config/sway/outputs.conf" ] || \
		: >"$TARGET/home/posterchan/.config/sway/outputs.conf"
	cat >"$TARGET/home/posterchan/.bash_profile" <<-'POSTERCHAN_PROFILE'
[[ -f ~/.bashrc ]] && . ~/.bashrc
if [ -z "$WAYLAND_DISPLAY" ] && [ "$XDG_VTNR" = 1 ]; then
	export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=sway MOZ_ENABLE_WAYLAND=1
	mkdir -p "$HOME/.local/state/posterchanos"
	exec sway >"$HOME/.local/state/posterchanos/sway.log" 2>&1
fi
POSTERCHAN_PROFILE
	printf '[Unit]\nWants=NetworkManager.service\nAfter=NetworkManager.service\n[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin posterchan --noclear %%I $TERM\n' \
		>"$TARGET/etc/systemd/system/getty@tty1.service.d/override.conf"
	# Wi-Fi is boot-critical. Enable it explicitly in the completed target rather than relying only
	# on the earlier services loop, which may have been interrupted before finalization.
	chroot "$TARGET" /bin/systemctl enable NetworkManager.service
	# This is a brand-new account tree, so its contents all belong to the account. Chowning only the
	# `sway` child left ~/.config itself root:root 0755; Electron then could not create its userData
	# directory and Chromium aborted with SIGTRAP before the first desktop window mapped.
	chroot "$TARGET" /bin/chown -R posterchan:posterchan /home/posterchan
	# RELEASE GATE, NOT A BEST-EFFORT CHECK. These are the exact omissions that otherwise produce a
	# technically booted machine at a tty and a stock splash, after the installer claimed success.
	# Check the target files themselves after every phase that can overwrite them.
	if ! grep -q 'exec sway' "$TARGET/home/posterchan/.bash_profile" 2>/dev/null; then
		echo -e "\033[1;31mPosterChan session profile was not installed — refusing to report success.\033[0m"
		return 1
	fi
	if ! grep -q -- '--autologin posterchan' \
		"$TARGET/etc/systemd/system/getty@tty1.service.d/override.conf" 2>/dev/null; then
		echo -e "\033[1;31mPosterChan autologin was not installed — refusing to report success.\033[0m"
		return 1
	fi
	if [ ! -e "$TARGET/etc/systemd/system/multi-user.target.wants/NetworkManager.service" ]; then
		echo -e "\033[1;31mNetworkManager was not enabled in the installed system — refusing to report success.\033[0m"
		return 1
	fi
	if ! grep -q '^Theme=posterchanos$' "$TARGET/etc/plymouth/plymouthd.conf" 2>/dev/null; then
		echo -e "\033[1;31mPosterChan boot splash was not selected — refusing to report success.\033[0m"
		return 1
	fi
	if [ "$(chroot "$TARGET" /usr/bin/stat -c %U /home/posterchan/.config 2>/dev/null)" != posterchan ]; then
		echo -e "\033[1;31mPosterChan profile directory is not writable by its session account — refusing to report success.\033[0m"
		return 1
	fi
	BOOT_ENTRY="$(find "$TARGET/boot/loader/entries" -maxdepth 1 -type f -name '*.conf' 2>/dev/null | sort | head -1)"
	if [ -z "$BOOT_ENTRY" ] || [ ! -s "$BOOT_ENTRY" ]; then
		echo -e "\033[1;31mNo systemd-boot entry was installed — refusing to report success.\033[0m"
		return 1
	fi
	if [ ! -s "$TARGET/boot/EFI/BOOT/BOOTX64.EFI" ]; then
		echo -e "\033[1;31mThe EFI fallback loader is missing — refusing to report success.\033[0m"
		return 1
	fi
	BOOT_INITRD="$(sed -n 's|^initrd[[:space:]]\+|/boot/|p' "$BOOT_ENTRY" 2>/dev/null | head -1)"
	if [ -z "$BOOT_INITRD" ] || ! chroot "$TARGET" /usr/bin/lsinitrd "$BOOT_INITRD" 2>/dev/null \
		| grep -q 'themes/posterchanos/posterchanos.plymouth'; then
		echo -e "\033[1;31mPosterChan boot splash is not embedded in the booted initramfs — refusing to report success.\033[0m"
		return 1
	fi
	# READ BACK THE ENCRYPTED BOOT CHAIN AT THE LAST POSSIBLE MOMENT. bootloader() validates its own
	# work, but later finalization steps used to overwrite files and still print Complete. A valid
	# splash inside an initramfs proves nothing about whether that image can open the root volume.
	if ! grep -q 'rd\.luks\.uuid=luks-' "$BOOT_ENTRY" 2>/dev/null \
		|| ! grep -Eq '[[:space:]]luks([[:space:]]|$)' "$TARGET/etc/crypttab" 2>/dev/null \
		|| ! chroot "$TARGET" /usr/bin/lsinitrd "$BOOT_INITRD" 2>/dev/null \
			| grep -q 'systemd-cryptsetup'; then
		echo -e "\033[1;31mEncrypted-root boot files are incomplete — refusing to report success.\033[0m"
		return 1
	fi
	if [ "$AUTO_DECRYPT" = "True" ] && { \
		! chroot "$TARGET" /usr/bin/lsinitrd "$BOOT_INITRD" 2>/dev/null | grep -q 'boot/keyfile.key'; \
	}; then
		echo -e "\033[1;31mAutomatic LUKS unlock was selected but its key is not in the booted initramfs.\033[0m"
		return 1
	fi
	if ! cmp -s "$INSTALLER_SRC" "$TARGET/usr/bin/gentoo.sh"; then
		echo -e "\033[1;31mThe target did not receive this PosterChanOS installer version — refusing to report success.\033[0m"
		return 1
	fi
	# Recovery needed root while the install was incomplete. At this point boot, accounts and the
	# graphical shell all succeeded, so disable direct root login. pc-provision-user atomically makes
	# the first key-backed person the administrator; everybody after them is an ordinary user.
	chroot $TARGET /usr/bin/passwd -l root
	rm -f $TARGET/setup.sh
	echo
	echo -e "\033[1;33mGentoo Installation Complete!\033[0m"
	echo
	echo
}

installPackages() {
	unmaskPackages
	/usr/bin/emerge -uDN $PACKAGES --autounmask-write
	/usr/sbin/etc-update -q --automode -5
	if /usr/bin/emerge -uDN $PACKAGES; then
		return 0
	fi

	# ONE BAD ATOM MUST NOT COST THE WHOLE DESKTOP, and it silently did.
	#
	# emerge refuses the entire set if a single name cannot be resolved, and nothing here checked:
	# buildGentoo carried straight on to finalizeInstall, the install reported success, and the
	# machine came up with a kernel, a shell session, a portal config — and no sway, no browser, no
	# OBS. The cause was one typo, `games-util/gamescope` for `gui-wm/gamescope`, and the only trace
	# was a line in the middle of a very long log.
	#
	# So a failure is retried package by package. What resolves gets installed, what does not is
	# NAMED — which is the difference between "the desktop is missing" and "these two names are
	# wrong". Slower, and it only runs on the path that was previously a total loss.
	echo -e "\033[1;31m◆ THE PACKAGE SET FAILED — RETRYING ONE AT A TIME ◆\033[0m"
	FAILED_PKGS=""
	for pkg in $PACKAGES; do
		if ! /usr/bin/emerge -uDN --autounmask-write "$pkg" >/dev/null 2>&1; then
			/usr/sbin/etc-update -q --automode -5 >/dev/null 2>&1
		fi
		if ! /usr/bin/emerge -uDN "$pkg"; then
			echo -e "\033[1;31m  ✗ $pkg\033[0m"
			FAILED_PKGS="$FAILED_PKGS $pkg"
		fi
	done
	if [ -n "$FAILED_PKGS" ]; then
		echo
		echo -e "\033[1;31m◆ THESE PACKAGES DID NOT INSTALL ◆\033[0m"
		echo -e "\033[1;31m $FAILED_PKGS\033[0m"
		echo -e "\033[1;33mEverything else did. Fix the names and re-run: gentoo.sh install-packages\033[0m"
		echo
		return 1
	fi
}

# Flathub is added, and nothing is installed from it. The list this used to carry was the KDE
# desktop's — konsole, dolphin, kcalc, kdenlive, Brava, Thunderbird — and PosterChanOS supplies its
# own equivalents. Native Steam comes from Portage; the remote remains the sane place for a person
# to get an app this OS does not ship.
installFlatpaks() {
	/usr/bin/flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
}

# ── NODATACOW FOR THE PATHS THAT ARE WRITTEN TO CONSTANTLY ──────────────────────────────────────
#
# A database, a VM image and a browser profile all do the same thing to btrfs: many small overwrites
# inside one large file. Copy-on-write turns each of those into a new extent, and the file ends up
# in tens of thousands of fragments — the write amplification and the seek cost are both real, and
# on the machine that IS the desktop it is felt as the whole UI stuttering.
#
# THE TRADE, stated here so nobody "fixes" it later: nodatacow also turns OFF checksums for those
# files, and effectively opts them out of compression. For a database that is the intended bargain —
# the database has its own integrity checks and its own page format — but it is a real loss of
# btrfs's own scrubbing, and it is why this is a short, named list rather than something applied
# broadly. It also means a nodatacow file inside a snapshot is CoW'd once on the next write anyway.
#
# TWO THINGS MAKE THE OBVIOUS `chattr -R +C <dir>` A SILENT NO-OP, and this used to do exactly that:
#
#   1. +C ONLY TAKES ON A ZERO-LENGTH FILE. On a file that already has extents the ioctl is either
#      refused or accepted and changes nothing about the data already written. What actually works
#      is setting +C on the DIRECTORY, which every file created in it afterwards inherits — so it
#      has to run BEFORE the data lands. `-R` over a populated tree reports success and converts
#      nothing, which is the worst possible outcome: it looks done.
#   2. IT IS BTRFS-ONLY. On ext4/xfs/zfs chattr fails, and that is not a reason to stop an install —
#      but a bare failure prints a scary error on every non-btrfs box for a tuning step that simply
#      does not apply there.
#
# So: check the filesystem, create the directory (a directory that does not exist yet is the ideal
# case — nothing to convert), set +C on the DIRECTORY, and then say honestly how many files were
# already there and are therefore still CoW. Converting those is `gentoo.sh btrfs-tweaks-rewrite`,
# which is deliberately a separate, explicit command: it rewrites files, and doing that under a
# running database is how you corrupt one.
nodatacow() {
	local dir="$1"
	local fs
	fs=$(stat -f -c %T "$dir" 2>/dev/null || stat -f -c %T "$(dirname "$dir")" 2>/dev/null)
	if [ "$fs" != "btrfs" ]; then
		echo "  skip $dir — ${fs:-unknown filesystem}, nodatacow is btrfs-only"
		return 0
	fi
	mkdir -p "$dir" 2>/dev/null
	if ! chattr +C "$dir" 2>/dev/null; then
		echo "  skip $dir — could not set +C (not btrfs, or no permission)"
		return 0
	fi
	# What is ALREADY in there keeps the extents it was written with. Counted and named rather than
	# glossed over, because "I ran the command" and "the data is nodatacow" are different facts.
	local n
	n=$(find "$dir" -type f 2>/dev/null | head -20000 | wc -l)
	if [ "$n" -gt 0 ]; then
		echo "  +C $dir — new files inherit it; $n existing file(s) keep copy-on-write"
		echo "      convert them with: gentoo.sh btrfs-tweaks-rewrite $dir   (service STOPPED)"
	else
		echo "  +C $dir — empty, so everything written here is nodatacow"
	fi
}

btrfsTweaks() {
	# The system's write-heavy stores. `/var/lib/postgresql` is the one that matters on a node
	# running the relay — the event store is a stream of small writes and is what the user named.
	DISABLE_COW=("/var/lib/postgresql" "/var/lib/mysql" "/var/lib/libvirt" \
	             "/var/lib/docker" "/volumes")

	echo -e "\033[1;36m[nodatacow on the write-heavy paths]\033[0m"
	for i in "${DISABLE_COW[@]}"; do
		nodatacow "$i"
	done
	# The per-user Electron profile is the OTHER write-heavy path on a PosterChanOS box, and it is
	# the busiest: the client's local relay lives in IndexedDB inside it. Measured on the test
	# laptop, ~/.config/posterchan-desktop was 453 MB of LevelDB and cache with no C attribute at
	# all. It cannot be done here because accounts are created when somebody signs in, long after
	# the installer has finished — pc-provision-user sets +C on it as it creates the home, which is
	# the one moment it is free.
	echo "  (per-user ~/.config/posterchan-desktop is handled by pc-provision-user, at sign-in)"
}

# CONVERT WHAT IS ALREADY THERE. Deliberately separate and never part of an install: the only way to
# give an existing file nodatacow is to write its contents into a NEW file inside a +C directory, so
# this rewrites every file in the tree. Under a running database that is data loss, which is why it
# asks, and why the message says to stop the service rather than assuming somebody did.
nodatacowRewrite() {
	local dir="$1"
	[ -n "$dir" ] || { echo "usage: gentoo.sh btrfs-tweaks-rewrite <dir>"; return 1; }
	[ -d "$dir" ] || { echo "$dir is not a directory"; return 1; }
	if [ "$(stat -f -c %T "$dir" 2>/dev/null)" != "btrfs" ]; then
		echo "$dir is not on btrfs — nothing to do"; return 0
	fi
	echo -e "\033[1;33mThis rewrites every file under $dir.\033[0m"
	echo "Anything using it MUST be stopped first — rewriting a live database corrupts it."
	read -p "Type the directory again to confirm: " -r confirm
	[ "$confirm" = "$dir" ] || { echo "not confirmed"; return 1; }

	chattr +C "$dir" 2>/dev/null
	local done=0 failed=0
	# cp to a NEW file inside the +C directory (so it inherits nodatacow), then rename over the
	# original. --preserve=all keeps the mode, owner and timestamps a database cares about;
	# --reflink=never is the point — a reflink would share the old CoW extents.
	while IFS= read -r -d '' f; do
		if cp --preserve=all --reflink=never "$f" "$f.nocow.$$" 2>/dev/null \
		   && mv -f "$f.nocow.$$" "$f" 2>/dev/null; then
			done=$((done + 1))
		else
			rm -f "$f.nocow.$$" 2>/dev/null
			failed=$((failed + 1))
		fi
	done < <(find "$dir" -type f -print0 2>/dev/null)
	echo "rewritten: $done   failed: $failed"
	[ "$failed" -eq 0 ]
}

liveOSrestore() {
	clear
	# WHERE THIS SCRIPT IS, NOT WHERE SOMEBODY HAPPENED TO BE STANDING.
	#
	# This was `SCRIPT=$(pwd)`, which is only right when the installer was started by typing
	# ./gentoo.sh in its own directory. On the live disc it is launched from the desktop entry --
	# `foot -e sh -c /usr/local/share/posterchanos/gentoo.sh` -- whose working directory is the live
	# user's home, so `$SCRIPT/gentoo.sh` named a file that was never there and the copy failed with
	# "cp: cannot stat 'gentoo.sh'". Reported from exactly that path: installing the live system to
	# a disk.
	#
	# PCOS_TREE already answers this at the top of the file, by looking where the script IS and then
	# at the two places the ISO puts it. `$0` is the last resort for a bare script run from a shell
	# with no tree around it.
	# PCOS_TREE and nothing else: it already looks where the script IS before it looks anywhere
	# else, so a second `$0` guess here would be the same question asked twice and answered
	# differently -- which is what the resolver exists to prevent.
	SCRIPT="$PCOS_TREE"
	INSTALL_TYPE=$(mount | grep ' / ')
	partitions
	systemMounts
	clear

	echo -e "\033[1;36m[Transferring Currenting Running OS from $LIVE_OS_DM to $HARD_DISK ]\033[0m"
	echo

	if [[ $BUILD_SERVER = *y* ]]; then
		read -p 'BTRFS Backup Volume Name: ' -e -i "/raid/gentoo-desktop.lan" BUILD_PATH
		sudo rsync -avz --delete --rsync-path='sudo rsync' -e ssh $USER@$BUILD_SERVER_ADDRESS:/$BUILD_PATH/ $RSYNC_EXCLUDES $TARGET/
	else
		sudo rsync -aHAX --delete / $RSYNC_EXCLUDES $TARGET/
		_rc=$?
		sudo rsync -aHAX --delete /boot/ $TARGET/boot/ || _rc=$?
	fi

	# ---------------------------------------------------------------- did the copy actually happen
	#
	# EVERY LATER STEP ASSUMES A ROOT FILESYSTEM IS THERE, and none of them check. When the copy
	# produced nothing the install carried on regardless and failed one step at a time, in a
	# different place each run: "/tmp/install/etc/os-release: no such file", then a plymouth theme
	# that could not be written, then a copy of the installer with nowhere to go. Three errors, one
	# cause, and none of them naming it.
	#
	# On the live disc there is a specific way for this to happen quietly: $TARGET is /tmp/install,
	# and /tmp on a live system is a tmpfs. If the partition was not mounted there, rsync writes into
	# RAM until it runs out -- so a failed MOUNT looks like a failed COPY looks like a broken script.
	if [ "${_rc:-0}" -ne 0 ] || [ ! -d "$TARGET/etc" ] || [ ! -d "$TARGET/usr" ]; then
		echo
		echo -e "${COLOR_YELLOW}The copy to $HARD_DISK did not complete — nothing was installed.${COLOR_RESET}"
		if ! mountpoint -q "$TARGET" 2>/dev/null; then
			echo -e "${COLOR_YELLOW}$TARGET is not a mount point, so this was writing into RAM.${COLOR_RESET}"
			echo -e "${COLOR_YELLOW}The disk was never mounted — check the partitioning step.${COLOR_RESET}"
		fi
		echo -e "${COLOR_YELLOW}Stopping here rather than building half a system on it.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return 1
	fi

	fstab
	# THE INSTALLED SYSTEM CARRIES THE INSTALLER, and a missing copy is not worth failing an install
	# over -- but it IS worth saying, because the machine then has no `gentoo.sh` to re-run and
	# nothing else says so.
	if [ -f "$SCRIPT/gentoo.sh" ]; then
		cp -f "$SCRIPT/gentoo.sh" $TARGET/usr/bin/ 2>/dev/null \
			|| echo -e "${COLOR_YELLOW}  could not copy the installer into the new system${COLOR_RESET}"
	else
		echo -e "${COLOR_YELLOW}  no gentoo.sh to copy — the installed system will not carry one${COLOR_RESET}"
	fi
	[ -f /tmp/disk ] && cp -f /tmp/disk $TARGET/etc/ 2>/dev/null

	finalizeInstall
	cd
}

# ── INSTALL THE LIVE IMAGE ONTO A DISK ────────────────────────────────────────────────────────────
#
# A SEPARATE OPTION FROM "Backup/Restore Live OS", DELIBERATELY.
#
# `liveOSrestore` clones a RUNNING INSTALLED SYSTEM onto a disk, and it is good at that. Booted from
# the ISO it is being asked to do a different job, and it fails in two ways that are not bugs in it:
#
#   * `rsync[sender] change_dir /boot failed: no such directory` -- a live boot has no populated
#     /boot to copy FROM. The kernel came off the disc, not out of a mounted boot partition.
#   * `delete_file: rmdir{boot} failed: device or resource busy` -- `--delete` then tries to remove
#     $TARGET/boot, which is the EFI partition the installer has just mounted there.
#
# Both are the same misunderstanding: on a live medium the SOURCE of the system and the SOURCE of the
# kernel are two different places. So this is its own path rather than a flag on that one, and
# `liveOSrestore` is left exactly as it was.
liveISOinstall() {
	clear
	echo -e "${COLOR_CYAN}═══════════════════════════════════════════════════════${COLOR_RESET}"
	echo -e "${COLOR_BOLD}  ⚡ INSTALL THIS LIVE IMAGE ONTO A DISK ⚡${COLOR_RESET}"
	echo -e "${COLOR_CYAN}═══════════════════════════════════════════════════════${COLOR_RESET}"
	echo

	# WHERE THE KERNEL IS, before anything is written. A live boot keeps the medium mounted, and
	# which path depends on the initramfs: dracut's live modules use /run/initramfs/live, and a disc
	# mounted by hand is anybody's guess. Found FIRST, because an install that copies 4GB and then
	# discovers it has no kernel has wasted the only thing that is slow here.
	local LIVEDIR=""
	local d
	for d in /run/initramfs/live /run/rootfsbase /mnt/cdrom /media/cdrom; do
		[ -d "$d/boot" ] && { LIVEDIR="$d"; break; }
	done
	# The squashfs itself may carry /boot -- it is a copy of the machine the ISO was built on -- and
	# that is the better source when it is there, because it is the kernel this userland was built
	# against rather than the one the disc happens to boot with.
	local KSRC=""
	if [ -n "$(ls -A /boot 2>/dev/null)" ]; then KSRC="/boot"
	elif [ -n "$LIVEDIR" ]; then KSRC="$LIVEDIR/boot"
	fi
	if [ -z "$KSRC" ]; then
		echo -e "${COLOR_YELLOW}No kernel found on this live medium — looked in /boot and${COLOR_RESET}"
		echo -e "${COLOR_YELLOW}/run/initramfs/live/boot. Nothing was written.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return 1
	fi
	echo -e "${COLOR_YELLOW}Kernel source: $KSRC${COLOR_RESET}"

	# THIS IS A ONE-CLICK INSTALLER. The old path silently assumed menu option 5 (Initialize Disk)
	# had already been run, even though the Start-menu launcher opens this function directly. A fresh
	# disk then had no LUKS mapper or FAT ESP; systemMounts failed, its status was ignored, and rsync
	# copied gigabytes into the live session's /tmp. Prepare the selected disk here and prove every
	# layer before copying one byte.
	local need
	for need in parted partprobe cryptsetup mkfs.btrfs mkfs.vfat rsync blkid mountpoint wipefs; do
		if ! command -v "$need" >/dev/null 2>&1; then
			echo -e "${COLOR_YELLOW}PosterChanOS installer requirement is missing: $need${COLOR_RESET}"
			echo -e "${COLOR_YELLOW}Nothing was written.${COLOR_RESET}"
			return 1
		fi
	done
	setDevices || return 1
	local layout_ok=0 mode="fresh"
	if [ -b "$EFI" ] && [ -b "$BTRFS" ] \
		&& [ "$(blkid -s TYPE -o value "$EFI" 2>/dev/null)" = "vfat" ] \
		&& cryptsetup isLuks "$BTRFS" >/dev/null 2>&1; then
		layout_ok=1
		read -r -p "A prepared encrypted layout exists on /dev/$HARD_DISK. Fresh erase or resume? [f/r]: " mode
		mode="${mode:-f}"
	fi
	if [ "$layout_ok" -eq 0 ] || [[ "$mode" = [fF]* ]]; then
		echo -e "${COLOR_YELLOW}This will erase every file on /dev/$HARD_DISK.${COLOR_RESET}"
		read -r -p "Erase /dev/$HARD_DISK and install PosterChanOS? [y/N]: " erase
		[[ "$erase" = [yY]* ]] || { echo "Install cancelled; nothing was written."; return 1; }
		readInstallPassword confirm || return 1
		prepareInstallDisk || return 1
	else
		readInstallPassword existing || return 1
		partitionDetection
		if [ "$(blkid -s TYPE -o value "$EFI" 2>/dev/null)" != "vfat" ] \
			|| ! cryptsetup isLuks "$BTRFS" >/dev/null 2>&1; then
			echo -e "${COLOR_YELLOW}The existing layout is not a usable FAT32 + LUKS install target.${COLOR_RESET}"
			return 1
		fi
	fi
	systemMounts || {
		echo -e "${COLOR_YELLOW}The encrypted root or EFI partition could not be mounted. Nothing was copied.${COLOR_RESET}"
		return 1
	}

	# ---------------------------------------------------------------- the system
	#
	# COPY THE SQUASHFS, NOT THE LIVE OVERLAY. liveOSrestore() correctly copies `/` when `/` is an
	# installed OS. On a LiveCD it is an overlay assembled for this boot; the complete immutable OS
	# tree is the mounted squashfs at /run/rootfsbase. Copying the overlay made the result depend on
	# its mount topology and produced a target with directory stubs but no usable systemd OS tree.
	local ROOTSRC="/run/rootfsbase"
	if [ ! -s "$ROOTSRC/usr/lib/systemd/systemd" ]; then
		echo -e "${COLOR_YELLOW}The LiveCD squashfs is not mounted at $ROOTSRC.${COLOR_RESET}"
		echo -e "${COLOR_YELLOW}Refusing to copy the transient overlay as an installed OS.${COLOR_RESET}"
		return 1
	fi
	# The squashfs carries no target ESP state. /boot is excluded and the matching kernel/initramfs
	# are installed separately below from the live medium.
	echo -e "${COLOR_CYAN}Copying the system — this is the slow part.${COLOR_RESET}"
	sudo rsync -aH --one-file-system --info=progress2 \
		--exclude=/boot/*** $RSYNC_EXCLUDES "$ROOTSRC/" $TARGET/
	local RC=$?

	# A directory is not an OS tree. Assert systemd itself was copied, or stop
	# while the live environment still exists and can explain the copy failure.
	if [ "$RC" -ne 0 ] || [ ! -x "$TARGET/usr/lib/systemd/systemd" ]; then
		echo
		echo -e "${COLOR_YELLOW}The copy did not complete — nothing was installed.${COLOR_RESET}"
		if ! mountpoint -q "$TARGET" 2>/dev/null; then
			echo -e "${COLOR_YELLOW}$TARGET is not a mount point, so this was writing into RAM.${COLOR_RESET}"
		fi
		read -p "Press enter key to Continue"
		return 1
	fi

	# ---------------------------------------------------------------- the kernel
	#
	# Copied SEPARATELY and from wherever it actually lives, which is the whole reason this function
	# exists. `/boot/*` is excluded above so this is the only thing that writes there, and the
	# EFI partition mounted at $TARGET/boot is never a delete target.
	echo -e "${COLOR_CYAN}Installing the kernel${COLOR_RESET}"
	# The ISO is commonly mounted as HFS+/ISO9660. Neither filesystem can supply Linux ACLs or
	# extended attributes, and rsync reports that as code 23 when -A/-X are requested even though
	# these boot files do not need either. That made this step abort with an EMPTY ESP before fstab
	# or the bootloader were written. Preserve the ordinary metadata and hard links only.
	sudo rsync -aH --exclude='initramfs*' --exclude='initrd*' "$KSRC"/ $TARGET/boot/ || {
		echo -e "${COLOR_YELLOW}The kernel did not copy — the disk would not boot.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return 1
	}

	# THE LIVE IMAGE MAY HAVE BEEN BUILT BY AN OLDER INSTALLER and therefore still carry the source
	# machine's /etc/dracut.conf. That file names the source LUKS UUID and asks for its unlock.sh and
	# keyfile, neither of which belongs to this new target. The first target dracut run is below,
	# while bootloader() creates the new disk's real key and configuration later in finalizeInstall.
	# Clear only the copied host policy now; packaged defaults under /usr/lib/dracut remain intact.
	sudo mkdir -p "$TARGET/etc/dracut.conf.d"
	sudo sh -c ': > "$1/etc/dracut.conf"' sh "$TARGET"
	sudo find "$TARGET/etc/dracut.conf.d" -maxdepth 1 -type f -name '*.conf' -delete

	# THE LIVE INITRAMFS IS THE ONE THING ON THAT MEDIUM THAT MUST NOT BE INSTALLED, hence the
	# excludes above. It is built to find a squashfs on a removable disc: put it on a hard drive and
	# the machine boots looking for the USB stick it was installed from, which is a failure that
	# looks like a broken install and is actually a correct initramfs doing its job in the wrong
	# place.
	#
	# The squashfs carries /lib/modules even though it carries no /boot (the ISO holds the kernel
	# separately), so the target has everything dracut needs to build a real one. Built INSIDE the
	# chroot, or it describes this live session's hardware and root device instead of the installed
	# machine's.
	# A MACHINE OF ITS OWN NEEDS AN IDENTITY OF ITS OWN, and it needs it BEFORE kernel-install runs.
	#
	# The ISO deliberately ships an EMPTY /etc/machine-id so every live boot generates a fresh one --
	# a duplicated id breaks journald, DHCP leases and systemd-boot's own /boot layout. But this
	# profile installs kernels the Boot Loader Spec way, where the entry token IS the machine-id:
	# `/boot/<machine-id>/<version>/linux`, which is exactly the layout on the machine this was
	# built from. Run against an empty one, kernel-install has no directory to write into.
	#
	# So the identity is minted first, in the target, and it is the installed machine's from then on.
	sudo mkdir -p "$TARGET/var/tmp"
	sudo chmod 1777 "$TARGET/var/tmp"
	sudo chroot $TARGET /usr/bin/systemd-machine-id-setup >/dev/null 2>&1 \
		|| sudo chroot $TARGET /bin/sh -c 'systemd-machine-id-setup' >/dev/null 2>&1 || true
	if [ ! -s "$TARGET/etc/machine-id" ]; then
		echo -e "${COLOR_YELLOW}Could not create the installed machine ID — refusing to build${COLOR_RESET}"
		echo -e "${COLOR_YELLOW}boot paths with an empty directory name.${COLOR_RESET}"
		return 1
	fi

	local KVER
	KVER="$(ls $TARGET/lib/modules 2>/dev/null | sort -V | tail -1)"
	if [ -n "$KVER" ]; then
		echo -e "${COLOR_CYAN}Building an initramfs for $KVER${COLOR_RESET}"
		# `kernel-install` first: this profile uses systemd-boot and the Boot Loader Spec layout, and
		# it is what puts a kernel where bootctl will find it. dracut alone is the fallback for a
		# target that does not have it.
		# AN INITRAMFS THAT CANNOT OPEN LUKS CANNOT FIND THE ROOT FILESYSTEM, and a machine that
		# cannot find its root drops to emergency mode -- which is the first half of what was
		# reported. This profile installs onto an encrypted disk every time, so `crypt` and `dm` are
		# not optional here the way dracut's autodetection treats them: built inside a chroot,
		# hostonly detection is looking at the LIVE session's block devices, not the target's.
		#
		# `cryptsetup` has to be in the target for the crypt module to build at all. The build log
		# for the ISO already showed the shape of that failure -- "Module 'systemd-cryptsetup'
		# depends on module 'crypt', which can't be installed" -- so it is checked rather than hoped
		# for, and said out loud when it is missing.
		if [ ! -x "$TARGET/sbin/cryptsetup" ] && [ ! -x "$TARGET/usr/sbin/cryptsetup" ]; then
			echo -e "${COLOR_YELLOW}  cryptsetup is not on the target — the initramfs cannot open${COLOR_RESET}"
			echo -e "${COLOR_YELLOW}  an encrypted root and the machine will boot to emergency mode${COLOR_RESET}"
		fi
		DRACUT_ADD="crypt systemd-cryptsetup dm rootfs-block"
		# KERNEL-INSTALL IS NOT USED HERE, AND THAT IS DELIBERATE: IT REFUSES TO RUN IN A CHROOT.
		#
		# `05-check-chroot.install` compares `/` against `/proc/1/root` and then, for a dracut
		# initramfs with no configured command line, prints "Dracut would fallback to using
		# /proc/cmdline, which is generally not what you want. Exiting..." and exits 1. Measured
		# by running the plugin's own predicate against the file the target actually carries:
		# /etc/dracut.conf comes off the ISO as a copy of the build machine's, where bootloader()
		# writes `kernel_cmdline+=` -- and the plugin greps for `^kernel_cmdline=`, which that
		# does not match. So the check sees nothing configured and the step exits 1, every time.
		#
		# It is RIGHT to refuse. `90-loaderentry.install` would have taken the boot options from
		# /proc/cmdline, and in a live session that reads `root=live:CDLABEL=... rd.live.image` --
		# a hard disk told to go looking for the USB stick it was installed from.
		#
		# What the old `|| dracut` fallback did instead was write an initramfs to
		# /boot/initramfs-$KVER.img, which is a path the bootloader step never reads: it derives
		# the kernel version by listing /boot/<machine-id>, finds nothing, and builds every path
		# below it out of an empty string.
		#
		# The Boot Loader Spec layout is a documented directory shape, so it is written directly.
		local MID
		MID="$(sudo cat $TARGET/etc/machine-id 2>/dev/null)"
		if [ -n "$MID" ] && [ -f "$TARGET/boot/vmlinuz" ]; then
			sudo mkdir -p "$TARGET/boot/$MID/$KVER"
			sudo cp -f "$TARGET/boot/vmlinuz" "$TARGET/boot/$MID/$KVER/linux"
			sudo chroot $TARGET /usr/bin/dracut --force --add "$DRACUT_ADD" \
				"/boot/$MID/$KVER/initrd" "$KVER" \
				|| echo -e "${COLOR_YELLOW}  dracut failed here — the bootloader step tries again${COLOR_RESET}"
		fi
		# SAID OUT LOUD EITHER WAY. Both halves fail silently, and the machine only mentions it at
		# the next boot, in emergency mode, which is not where anybody can read a scrollback.
		if [ -f "$TARGET/boot/$MID/$KVER/linux" ] && [ -f "$TARGET/boot/$MID/$KVER/initrd" ]; then
			echo -e "${COLOR_CYAN}  kernel and initramfs are in /boot/$MID/$KVER${COLOR_RESET}"
		else
			echo -e "${COLOR_YELLOW}  /boot/$MID/$KVER is incomplete — this machine will not boot${COLOR_RESET}"
		fi
	else
		echo -e "${COLOR_YELLOW}  no /lib/modules on the target — the bootloader step will have to${COLOR_RESET}"
		echo -e "${COLOR_YELLOW}  build the initramfs${COLOR_RESET}"
	fi

	# THE LIVE SESSION'S OWN ACCOUNT DOES NOT BELONG ON AN INSTALL. `live` is passwordless and in
	# wheel with NOPASSWD sudo, which is right for a disc anybody can pick up and wrong for a machine
	# somebody keeps. accounts() (via finalizeInstall) creates the real user.
	sudo sed -i '/^live:/d' $TARGET/etc/passwd $TARGET/etc/shadow $TARGET/etc/group 2>/dev/null
	sudo rm -f $TARGET/etc/sudoers.d/live 2>/dev/null
	sudo rm -rf $TARGET/home/live 2>/dev/null
	# …and the autologin that names it, or the installed machine tries to log in an account that is
	# no longer there — which is a login prompt, and the exact failure the ISO builder was fixed for.
	sudo rm -f $TARGET/etc/systemd/system/getty@tty1.service.d/override.conf 2>/dev/null

	# ROOT MUST BE ABLE TO LOG IN BEFORE ANYTHING ELSE IS TRIED.
	#
	# The ISO ships /etc/shadow with root LOCKED (`!`), which is right for a disc anybody can pick up
	# and is copied straight onto the installed machine by the rsync above. `accounts` sets a
	# password later -- it is run from setup.sh by finalizeInstall, and its own comment says locking
	# root here is "catastrophic... the alternative is a brick" -- but that is at the END of a chain
	# of steps, and if any of them fails the machine is left with a locked root and no way in.
	#
	# That is exactly what was reported: an installed system dropping to emergency mode with
	# "Cannot open access to console, the root account is locked". Two failures, and the second is
	# what turned a fixable boot problem into a brick: systemd's emergency shell refuses to start
	# for a locked root, so the one tool for diagnosing the first failure was unavailable.
	#
	# Done HERE, immediately after the copy, so it is true even if everything after it fails.
	echo -e "${COLOR_CYAN}Unlocking root on the installed system${COLOR_RESET}"
	echo "root:$ROOT_PASSWORD" | sudo chroot $TARGET /usr/sbin/chpasswd 2>/dev/null \
		|| echo -e "${COLOR_YELLOW}  could not set a root password — emergency mode will refuse a shell${COLOR_RESET}"

	fstab
	if [ -f "$PCOS_TREE/gentoo.sh" ]; then
		sudo cp -f "$PCOS_TREE/gentoo.sh" $TARGET/usr/bin/ 2>/dev/null || true
	elif [ -f /usr/local/share/posterchanos/gentoo.sh ]; then
		sudo cp -f /usr/local/share/posterchanos/gentoo.sh $TARGET/usr/bin/ 2>/dev/null || true
	fi
	[ -f /tmp/disk ] && sudo cp -f /tmp/disk $TARGET/etc/ 2>/dev/null

	finalizeInstall || return 1
	# A kernel-launched or otherwise unattended installer may intentionally have no HOME. `cd` with
	# no destination then fails after every release gate has passed and turns a complete install into
	# exit status 1. Nothing below needs a directory change; report the verified result explicitly.
	return 0
}

backupOS() {
	clear
	echo
	echo -e "\033[1;36m[Backup OS to Build Server via Rsync]\033[0m"
	echo
	clear
	read -p 'BTRFS Backup Destination: ' -e -i "/raid/gentoo-desktop.lan" BUILD_PATH
	sudo rsync -avz --delete --rsync-path='sudo rsync' -e ssh / $RSYNC_EXCLUDES $USER@$BUILD_SERVER_ADDRESS:$BUILD_PATH/
}

btrfs_filesytem() {
	# Resume is a supported installer choice. Creating an existing subvolume returns failure, so the
	# old unconditional sequence made every resumed install stop before the copy. Verify each path is
	# a subvolume and create only what is absent; an ordinary directory at one of these names remains
	# an error rather than being mistaken for the intended layout.
	local sub
	for sub in "@$ROOT_NAME" "@.snapshots" "@libvirt" "@home" "@root" "@swap"; do
		btrfs subvolume show "$TARGET/$sub" >/dev/null 2>&1 \
			|| btrfs subvolume create "$TARGET/$sub" || return 1
	done
	if [ ! -f "$TARGET/@swap/swap" ]; then
		if [ -z "${SWAP_SIZE}" ]; then
			btrfs filesystem mkswapfile --size "$(free -m | awk '{print $2}' | tail -2 | head -1)m" "$TARGET/@swap/swap" || return 1
		else
			btrfs filesystem mkswapfile --size "$SWAP_SIZE" "$TARGET/@swap/swap" || return 1
		fi
	fi
	echo
	echo -e "\033[1;33mBinding BTRFS Root\033[0m"
	echo
	umount "$TARGET" || return 1
	mount -o "$COMPRESSION,subvol=@$ROOT_NAME" "$ROOT_MAPPER_NAME" "$TARGET" || return 1
}

services() {
	echo "[Unit]" >/etc/systemd/system/powertop.service
	echo "Description=Powertop tunings" >>/etc/systemd/system/powertop.service
	echo "[Service]" >>/etc/systemd/system/powertop.service
	echo "Type=oneshot" >>/etc/systemd/system/powertop.service
	echo "ExecStartPre=/usr/bin/cpupower frequency-set -d 400mhz -u 1.5ghz -g powersave" >>/etc/systemd/system/powertop.service
	echo "ExecStart=/usr/sbin/powertop --auto-tune" >>/etc/systemd/system/powertop.service
	echo "[Install]" >>/etc/systemd/system/powertop.service
	echo "WantedBy=multi-user.target" >>/etc/systemd/system/powertop.service

	torService

	for i in "${SERVICES[@]}"; do
		systemctl enable $i
	done
}

# ── TOR, ON FROM THE FIRST BOOT ───────────────────────────────────────────────────────────────────
#
# A system daemon, which is NOT the same thing as the desktop app's own bundled tor. That one is
# per-app and dies with the app; this is a SOCKS port every program on the machine can use, up
# before anybody logs in.
#
# GeoIPFile IS LOAD-BEARING. Without it tor cannot resolve a `{cc}` country code at all -- and it
# does not fail loudly: it bootstraps to 100%, reports itself healthy, and silently ignores the
# country restriction. A configuration that appears to work and does not is worse here than one that
# refuses to start, because the whole point of asking for a country is that the traffic goes there.
#
# StrictNodes goes WITH ExitNodes and nowhere else: it turns the preference into a requirement, so
# tor fails to build a circuit rather than quietly leaving the country when it cannot.
#
# A NOTE ON ENTRY NODES, since this was asked for explicitly. Pinning entry guards by country is a
# real reduction in anonymity -- guards are meant to be few, stable and randomly chosen, and picking
# them by geography narrows the set an observer has to watch -- and on a slow day it can make
# bootstrapping take much longer. It is written here because it was asked for, and it is one line to
# remove.
torService() {
	mkdir -p /etc/tor
	# The file is rewritten rather than appended to, so re-running the installer cannot end up with
	# two ExitNodes lines -- where tor takes the LAST and the visible first one is a lie.
	cat >/etc/tor/torrc <<-'TORRC'
		# PosterChanOS. Managed by gentoo.sh -- edits here are replaced on reinstall.
		SocksPort 9050
		# Country-restricted entry and exit. GeoIPFile is what makes {us} mean anything; without it
		# tor bootstraps to 100% and ignores both lines.
		GeoIPFile /usr/share/tor/geoip
		GeoIPv6File /usr/share/tor/geoip6
		EntryNodes {us}
		ExitNodes {us}
		StrictNodes 1
	TORRC
	chmod 0644 /etc/tor/torrc
	# Gentoo's net-vpn/tor ships tor.service; enabling it here rather than in SERVICES keeps the
	# whole of this feature -- package, config and unit -- in one place somebody can read at once.
	systemctl enable tor 2>/dev/null || true
}

_pc_record_plymouth_theme() {
	# plymouth-set-default-theme is inconsistent across releases: some versions update
	# plymouthd.conf, while others only maintain the default-theme link.  Dracut honours the config
	# file and finalization verifies it, so record the selected theme explicitly after the selector
	# succeeds.  Preserve every unrelated daemon option already present.
	local ROOT CONF
	ROOT="${TARGET%/}"
	CONF="${ROOT}/etc/plymouth/plymouthd.conf"
	mkdir -p "$(dirname "$CONF")"
	if [ -f "$CONF" ] && grep -q '^[[:space:]]*Theme[[:space:]]*=' "$CONF"; then
		sed -i 's/^[[:space:]]*Theme[[:space:]]*=.*/Theme=posterchanos/' "$CONF"
	else
		[ -s "$CONF" ] || printf '[Daemon]\n' >"$CONF"
		printf 'Theme=posterchanos\n' >>"$CONF"
	fi
}

_pc_select_plymouth_theme() {
	# Do not make a correct install depend on plymouth-set-default-theme's distro/version-specific
	# bookkeeping. Plymouth's real inputs are the default.plymouth link and plymouthd.conf; write
	# both ourselves, then let the helper update any extra metadata when it can. The final dracut
	# build below is what embeds the result in the image that systemd-boot actually loads.
	local ROOT THEME DEFAULT
	ROOT="${TARGET%/}"
	THEME="${ROOT}/usr/share/plymouth/themes/posterchanos/posterchanos.plymouth"
	DEFAULT="${ROOT}/usr/share/plymouth/themes/default.plymouth"
	[ -s "$THEME" ] || return 1
	mkdir -p "$(dirname "$DEFAULT")"
	ln -sfn posterchanos/posterchanos.plymouth "$DEFAULT" || return 1
	if [ -n "$TARGET" ] && [ "$TARGET" != "/" ]; then
		chroot "$TARGET" /usr/bin/plymouth-set-default-theme posterchanos >/dev/null 2>&1 || true
	else
		plymouth-set-default-theme posterchanos >/dev/null 2>&1 || true
	fi
	# Some Plymouth releases rewrite plymouthd.conf using their own default (or remove the explicit
	# Theme line) even when the requested theme exists.  Record our selection AFTER the helper so the
	# installed file and the initramfs built immediately below agree.
	_pc_record_plymouth_theme || return 1
	# The helper may also rewrite default.plymouth. It has finished now, so restore one canonical,
	# deterministic link that no later command in this install can mutate.
	ln -sfn posterchanos/posterchanos.plymouth "$DEFAULT" || return 1
	[ "$(readlink "$DEFAULT" 2>/dev/null)" = "posterchanos/posterchanos.plymouth" ] \
		&& grep -q '^Theme=posterchanos$' "${ROOT}/etc/plymouth/plymouthd.conf"
}

plymouthTheme() {
	# The boot splash. Plymouth is already in BASE_PACKAGES; what it lacks is a theme that is ours,
	# and the stock one is not merely off-brand here — this boot asks for a LUKS PASSPHRASE, and a
	# default theme draws that prompt in colours it inherited from somewhere else. On a near-black
	# background that can leave the prompt invisible, and a person is then typing a disk password at
	# a screen that looks frozen. The theme draws the prompt itself for that reason.
	echo -e "\033[1;33m◆ BOOT SPLASH ◆\033[0m"
	# WHERE THE THEME IS depends on who is calling. From the installer on the live system it sits
	# beside the script; from inside the chroot `$0` is /usr/bin/gentoo.sh and there is no theme
	# next to it. Both are tried, and a miss is stated rather than skipped — an installer that
	# quietly leaves the stock splash looks identical to one that set ours.
	SRC="$PCOS_TREE/plymouth/posterchanos"
	[ -d "$SRC" ] || SRC="/tmp/plymouth/posterchanos"
	[ -d "$SRC" ] || SRC="${TARGET}/usr/share/posterchan/plymouth/posterchanos"
	[ -d "$SRC" ] || SRC="/usr/share/posterchan/plymouth/posterchanos"
	if [ ! -d "$SRC" ]; then
		echo -e "\033[1;31mno splash theme found at $SRC — leaving the default\033[0m"
		return 0
	fi
	DEST="${TARGET}/usr/share/plymouth/themes/posterchanos"
	mkdir -p "$DEST"
	cp -f "$SRC"/* "$DEST"/
	_pc_select_plymouth_theme || return 1
}

posterchanShell() {
	# PosterChan as the SHELL: sway starts, and the only thing it launches is the PosterChan desktop
	# app, fullscreen on the background layer. Everything else the person opens — a browser, a game,
	# a terminal — is an ordinary client that PosterChan places over its own desktop through the IPC.
	echo -e "\033[1;33m◆ POSTERCHAN SHELL ◆\033[0m"

	# THE SESSION ACCOUNT, not a named human — see accounts(). Nobody is baked into the image; the
	# people who use the machine get accounts when they sign in with a key. Defined FIRST because
	# the sudoers rule below names it, and an empty subject there is a rule that grants nothing to
	# nobody while looking perfectly well formed.
	SHELL_USER="posterchan"
	id -u "$SHELL_USER" >/dev/null 2>&1 || SHELL_USER="${USER:-posterchan}"
	_configure_shell_session() {
		local GETTY_DIR="${TARGET}/etc/systemd/system/getty@tty1.service.d"
		mkdir -p "$GETTY_DIR" "${TARGET}/home/$SHELL_USER/.config/sway"
		[ -e "${TARGET}/home/$SHELL_USER/.config/sway/outputs.conf" ] || \
			: >"${TARGET}/home/$SHELL_USER/.config/sway/outputs.conf"
		# The desktop asks NetworkManager on its first frame. multi-user services and getty otherwise
		# start in parallel, so a fast SSD can launch the welcome screen before nmcli has a D-Bus
		# service and falsely report that the computer has no network hardware.
		printf '[Unit]\nWants=NetworkManager.service\nAfter=NetworkManager.service\n[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin %s --noclear %%I $TERM\n' \
			"$SHELL_USER" >"$GETTY_DIR/override.conf"
		cat >"${TARGET}/home/$SHELL_USER/.bash_profile" <<-'PROFILE'
[[ -f ~/.bashrc ]] && . ~/.bashrc
if [ -z "$WAYLAND_DISPLAY" ] && [ "$XDG_VTNR" = 1 ]; then
	export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=sway MOZ_ENABLE_WAYLAND=1
	mkdir -p "$HOME/.local/state/posterchanos"
	exec sway >"$HOME/.local/state/posterchanos/sway.log" 2>&1
fi
PROFILE
		chown -R "$SHELL_USER:$SHELL_USER" "${TARGET}/home/$SHELL_USER/.bash_profile" \
			"${TARGET}/home/$SHELL_USER/.config" 2>/dev/null || true
	}

	# NOTHING HERE MAY PULL WEBKIT — masked so a future dependency FAILS rather than costing hours.
	# webkit-gtk is one of the longest builds in the tree, and the way you find out you need it is
	# that an install which looked nearly finished sits on one package all night. A mask turns that
	# into an error at dependency-resolution time, with the name of whatever asked for it. The
	# browser here is firefox-BIN, which is prebuilt and pulls none of this.
	mkdir -p ${TARGET}/etc/portage/package.mask
	# A BARE atom masks EVERY version and slot of a package, which is what is wanted here — and it
	# is also why `net-libs/webkit-gtk-6` was wrong. There is no such package: the GTK4/soup3 webkit
	# is a SLOT of net-libs/webkit-gtk. Portage reads the trailing `-6` as a VERSION, a versioned
	# atom is invalid without an operator, and the result was `Invalid atom in
	# /etc/portage/package.mask/posterchanos: net-libs/webkit-gtk-6` printed by every portage command
	# on the machine — while the line masked nothing at all. The line above it already covers slot 6.
	cat >${TARGET}/etc/portage/package.mask/posterchanos <<-'MASK'
	# PosterChanOS: no HTML engine may be built from source on this profile.
	# Bare atoms on purpose: each masks every version AND every slot of its package.
	net-libs/webkit-gtk
	www-client/chromium
	dev-qt/qtwebengine
	MASK

	# SOUND, ENABLED FOR EVERY USER — INCLUDING ONES THAT DO NOT EXIST YET.
	#
	# Gentoo ships the PipeWire user services disabled, and `fixSound` turns them on with
	# `systemctl --user`, which acts on the account running it and nothing else. That is fine on a
	# machine with one named human and wrong here: accounts are created when somebody signs in with
	# a key, long after the installer has finished, and each would come up silent with no obvious
	# reason why. `--global` writes the enablement into /etc/systemd/user, where every session that
	# has ever been or will be created picks it up.
	systemctl --global disable pulseaudio.socket pulseaudio.service >/dev/null 2>&1
	systemctl --global enable pipewire.socket pipewire-pulse.socket wireplumber.service >/dev/null 2>&1

	# THE BACKLIGHT, WRITABLE WITHOUT ROOT. /sys/class/backlight/*/brightness is root-owned, so a
	# session can read the brightness and not change it — a slider that moves and does nothing.
	# brightnessctl is the usual answer and is NOT IN THE GENTOO TREE, so this is the answer: hand
	# the file to the `video` group, which pc-provision-user already puts every account in.
	mkdir -p /etc/udev/rules.d
	# RUN+="chgrp/chmod", not GROUP=/MODE=. Those assignments apply to the DEVICE NODE in /dev, and
	# a backlight has none — what needs relaxing is a sysfs ATTRIBUTE file, /sys/class/backlight/*/
	# brightness, which udev will only touch by running something. Tried the tidy-looking way first
	# and the file stayed root:root 0644 with the rule loaded and matching.
	cat >/etc/udev/rules.d/90-posterchan-backlight.rules <<-'UDEV'
	ACTION=="add", SUBSYSTEM=="backlight", RUN+="/bin/chgrp video /sys/class/backlight/%k/brightness"
	ACTION=="add", SUBSYSTEM=="backlight", RUN+="/bin/chmod g+w /sys/class/backlight/%k/brightness"
	# Keyboard backlights are the same problem in a different subsystem.
	ACTION=="add", SUBSYSTEM=="leds", KERNEL=="*kbd_backlight", RUN+="/bin/chgrp video /sys/class/leds/%k/brightness"
	ACTION=="add", SUBSYSTEM=="leds", KERNEL=="*kbd_backlight", RUN+="/bin/chmod g+w /sys/class/leds/%k/brightness"
	UDEV
	# An `add` rule does not fire for hardware that is already present, so a rule installed after
	# boot changes nothing until the next one. Triggered explicitly, or the first session after an
	# install has a brightness key that does nothing and no way to tell why.
	udevadm control --reload >/dev/null 2>&1
	udevadm trigger --action=add --subsystem-match=backlight >/dev/null 2>&1
	udevadm trigger --action=add --subsystem-match=leds >/dev/null 2>&1

	# THE POWER MODE, WRITABLE WITHOUT ROOT — the same problem as the backlight, one directory over.
	# /sys/firmware/acpi/platform_profile is root:root 0644, so the panel can READ that this machine
	# offers low-power/balanced/performance and cannot select one: a row of buttons that report an
	# error. power-profiles-daemon is the usual answer and it is a daemon, a package, and a polkit
	# policy to replace three lines.
	#
	# NOT a udev rule: /sys/firmware/acpi is not a device and no `add` event is ever emitted for it,
	# so there is nothing for a rule to match. tmpfiles runs on every boot regardless, which is also
	# what makes it right for a file the kernel recreates.
	#
	# `video`, the group pc-provision-user already puts every account in — the same grant as the
	# brightness, and for the same reason: it is a comfort setting on the machine in front of you,
	# not a privilege. The cpufreq governor is the fallback path power.js takes on hardware with no
	# ACPI profile, and it needs the same treatment per policy.
	mkdir -p /etc/tmpfiles.d
	cat >/etc/tmpfiles.d/posterchan-power.conf <<-'TMPF'
	z /sys/firmware/acpi/platform_profile 0664 root video -
	z /sys/devices/system/cpu/cpufreq/policy*/scaling_governor 0664 root video -
	z /sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference 0664 root video -
	TMPF
	# Applied now as well as at every boot, or the first session after an install has power buttons
	# that do nothing and nothing to say why — the same trap as the un-triggered udev rule above.
	systemd-tmpfiles --create /etc/tmpfiles.d/posterchan-power.conf >/dev/null 2>&1

	mkdir -p /etc/sway
	cat >/etc/sway/config <<-'SWAY'
	# PosterChanOS — the shell owns the screen; PosterChan decides what goes where.
	set $mod Mod4


	# The desktop itself. Not a layer-shell surface: Electron cannot make one, and a fullscreen
	# window at the bottom of the stack is the same thing from the person's side, with the whole
	# client working unmodified in a browser and the APK as well.
	# STARTED BY A LAUNCHER, NOT DIRECTLY, because `for_window` cannot be relied on for this window.
	# An X11 client sets WM_CLASS AFTER it maps, so sway evaluates criteria against a window with no
	# class yet: every rule looks right in the file and none of them match, and the shell ends up
	# floating at 1280x860 in the middle of the screen. Electron picks X11 unless told otherwise, and
	# whether it is told depends on a flag surviving a wrapper and an AppRun. pc-shell-start finds
	# the window FIRST and pins it second — the same order wm.js uses for anything it launches, and
	# for the same reason: an app that has not appeared cannot be placed.
	# THE SESSION'S ENVIRONMENT HAS TO REACH SYSTEMD, OR SCREEN RECORDING DOES NOT EXIST.
	#
	# xdg-desktop-portal is a SYSTEMD USER SERVICE, D-Bus-activated. It does not inherit this
	# session's environment — it inherits `systemd --user`'s, which is empty of it — so it starts
	# with no XDG_CURRENT_DESKTOP and no WAYLAND_DISPLAY. It then has no desktop name to match, so
	# `sway-portals.conf` selects nothing and `UseIn=…;sway;…` in wlr.portal matches nothing, so the
	# wlroots backend is never loaded and the portal exposes NO ScreenCast interface at all.
	#
	# Measured, exactly that way: `org.freedesktop.DBus.Error.InvalidArgs: No such interface
	# "org.freedesktop.portal.ScreenCast"`, on a machine with xdg-desktop-portal-wlr installed,
	# pipewire running, a correct portals.conf and OBS 32 ready to go. OBS shows a screen-capture
	# source that can list nothing, which reads as an OBS problem and is not one. The desktop app's
	# own file dialog fails the same way, for the same reason, in the same breath — "No such
	# interface org.freedesktop.portal.FileChooser" is in the shell's log.
	#
	# BOTH lines, and they are not the same line twice: `import-environment` fills in systemd's user
	# manager (which is what starts the portal), `dbus-update-activation-environment` fills in the
	# D-Bus activation environment (which is what starts anything D-Bus launches directly). A
	# session with one and not the other works for half the things that need it.
	#
	# FIRST in this file, before anything that could activate a portal — the shell below opens a
	# file dialog and asks about screen capture, and a portal started with the wrong environment
	# keeps it for the life of the session.
	exec_always --no-startup-id systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE SWAYSOCK
	exec_always --no-startup-id dbus-update-activation-environment --systemd WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE SWAYSOCK
	# …and a portal that was already up holds the OLD environment, so it is restarted rather than
	# left to answer "no such capture" for the rest of the session. Failure is ignored on purpose:
	# on a machine where these units are not installed there is nothing to restart and nothing wrong.
	exec_always --no-startup-id systemctl --user try-restart xdg-desktop-portal xdg-desktop-portal-wlr

	# THE DISPLAY TURNS ITSELF OFF. Two minutes by default, and `pc-idle set <seconds>` changes it
	# (0 = never) -- read from a file rather than compiled into this config, which portage owns and
	# etc-update replaces on upgrade.
	exec_always --no-startup-id /usr/local/bin/pc-idle
	exec_always --no-startup-id /usr/local/bin/pc-shell-start

	# Windows are PLACED by PosterChan over its IPC, so the compositor must not lay them out itself.
	# A tiled window ignores position and size — the desktop would move things and nothing would
	# happen, silently.
	# EVERYTHING FLOATS ABOVE THE SHELL, AND THE SHELL DOES NOT.
	#
	# Without these rules every app TILES — and tiling against a window that is fullscreen gives the
	# newcomer zero space: Firefox launches, appears in the tree, and is 0x0. Nothing on screen, no
	# error, a browser that "does not run".
	#
	# The exclusion is done by floating everything and then un-floating the shell, rather than by a
	# negative lookahead, which sway's pcre2 criteria will not compile. It is ordered: later rules
	# win. This failed once before, when the shell arrived through XWayland and set WM_CLASS AFTER
	# mapping — the rules were evaluated against a window with no class and none matched. It comes up
	# as a native Wayland client now (ELECTRON_OZONE_PLATFORM_HINT in the wrapper), so `app_id` is
	# there at map time; the class line stays as the belt to that braces, and pc-shell-start forcing
	# fullscreen is the third.
	for_window [app_id=".*"] floating enable
	for_window [class=".*"] floating enable
	for_window [app_id="posterchan-desktop"] floating disable, border none
	for_window [class="posterchan-desktop"] floating disable, border none

	# A floating window with no geometry of its own gets something usable rather than whatever the
	# client asked for, which for a browser is often a 200x200 stub until it finishes starting.
	for_window [app_id="firefox"] resize set 1400 900, move position center
	for_window [class="firefox"] resize set 1400 900, move position center

	# Real applications keep compositor title bars and borders. Their visible frame and input surface
	# are therefore one object across resizing, maximising and monitor handoff.
	default_border none
	default_floating_border normal 3
	gaps inner 0
	gaps outer 0

	# Nothing draws over the desktop uninvited — no compositor wallpaper, no status bar. PosterChan
	# is the wallpaper and the taskbar.
	output * bg #000000 solid_color
	# Per-machine monitor arrangement written atomically by System Settings → Displays.
	include ~/.config/sway/outputs.conf
	# Hold Super and drag anywhere in a native app, including across monitor boundaries.
	floating_modifier $mod normal

	# Application switching is compositor-owned, so it keeps working while a native app has focus or
	# the desktop renderer is recovering.
	bindsym --no-repeat Mod1+Tab exec /usr/local/bin/pc-window-cycle next
	bindsym --no-repeat Mod1+Shift+Tab exec /usr/local/bin/pc-window-cycle previous

	# The one binding that is not PosterChan's to take: a way out when the shell is not running.
	# THE LAPTOP'S OWN KEYS. A desktop that ignores the volume and brightness keys on the keyboard
	# in front of you is not one — and these have to obey the same limits as the on-screen controls,
	# which is why they go through pc-key rather than calling wpctl with different numbers.
	#
	# --locked so they work with the screen locked (volume and brightness are not secrets), and
	# --no-repeat is deliberately NOT set: holding a key should ramp.
	bindsym --locked XF86AudioRaiseVolume  exec /usr/local/bin/pc-key volume-up
	bindsym --locked XF86AudioLowerVolume  exec /usr/local/bin/pc-key volume-down
	bindsym --locked XF86AudioMute         exec /usr/local/bin/pc-key mute
	bindsym --locked XF86AudioMicMute      exec /usr/local/bin/pc-key mic-mute
	bindsym --locked XF86MonBrightnessUp   exec /usr/local/bin/pc-key brightness-up
	bindsym --locked XF86MonBrightnessDown exec /usr/local/bin/pc-key brightness-down

	# Playback keys go to the PAGE, not to a system tool: what is playing is the client's music
	# library, which no external player can see — the tracks are encrypted blobs only it can
	# decrypt. pc-key reaches it over MPRIS with `busctl`, which systemd already ships, rather than
	# pulling in playerctl to do the same thing.
	bindsym --locked XF86AudioPlay exec /usr/local/bin/pc-key play-pause
	bindsym --locked XF86AudioNext exec /usr/local/bin/pc-key next
	bindsym --locked XF86AudioPrev exec /usr/local/bin/pc-key previous

	bindsym $mod+Shift+e exec swaynag -t warning -m 'Exit PosterChanOS?' -B 'Yes' 'swaymsg exit'
	# THE SAME TWO BINDINGS THE SHELL PACKAGE SHIPS. They drifted: this file still opened `foot` on
	# Alt+Return long after the overlay's copy had been changed to raise PosterChan's own terminal,
	# so a machine installed from the ISO got the old behaviour and one updated through the package
	# got the new one -- reported as "win + enter not loading PosterChan terminal on PosterChanOS",
	# on an install where the fix had been made and shipped to the other copy.
	bindsym Mod1+Return exec swaymsg -t send_tick pc:terminal
	bindsym Ctrl+Mod1+Delete exec swaymsg -t send_tick pc:tasks
	bindsym $mod+Shift+Return exec foot

	# ── MORE THAN ONE SCREEN ───────────────────────────────────────────────────────────────────────
	#
	# sway already ARRANGES extra outputs (left to right, in the order it finds them) and `output *`
	# already gives each one a background, so a second monitor lights up on its own. What was missing
	# is any way to REACH it: this session ships no window-management bindings at all -- every app is
	# a floating window opened from the desktop -- so a plugged-in monitor was a lit screen you could
	# not focus, could not move anything onto, and could not launch anything from.
	#
	# Direction words, not output names. `focus output right` follows whatever the arrangement
	# actually is, so it keeps working when a monitor is unplugged, moved, or was never there --
	# where a binding naming HDMI-A-1 is dead on a laptop with nothing attached.
	bindsym $mod+Left  exec /usr/local/bin/pc-window-snap left
	bindsym $mod+Right exec /usr/local/bin/pc-window-snap right
	bindsym $mod+Up    exec /usr/local/bin/pc-window-snap max
	bindsym Ctrl+$mod+Left  focus output left
	bindsym Ctrl+$mod+Right focus output right
	bindsym Ctrl+$mod+Up    focus output up
	bindsym Ctrl+$mod+Down  focus output down
	# The window goes with you: moved to the next screen AND followed, because a window that leaves
	# the screen you are looking at with the focus staying behind reads as having closed it.
	bindsym $mod+Shift+Left  move container to output left,  focus output left
	bindsym $mod+Shift+Right move container to output right, focus output right
	bindsym $mod+Shift+Up    move container to output up,    focus output up
	bindsym $mod+Shift+Down  move container to output down,  focus output down
	# Closing one, which nothing else here offered. The compositor draws no chrome -- PosterChan does
	# -- so a floating app has no titlebar and can only be closed from inside itself, and not every
	# app has a way.
	bindsym $mod+q kill
	bindsym Mod1+F4 kill

	# THE DESKTOP ITSELF STAYS PUT. It is maximized on the output it started on, and `focus output`
	# above can move the FOCUS to a second screen while the shell stays where it is -- which is what
	# makes the second screen a place to put windows rather than a second copy of the desktop. One
	# shell serves both: its taskbar lists every window the compositor has, on either screen.

	# THE SUPER KEY OPENS THE START MENU — from anywhere, including out of a full-screen browser.
	#
	# The shell has its own handler for this key, and it can only ever fire when the SHELL has the
	# keyboard. That is the wrong half: you press Super to leave whatever you are in, so the
	# keyboard almost always belongs to firefox or a game at that moment, and the desktop never sees
	# the key at all. A binding can only run a command, never call into us — so it broadcasts a
	# TICK, which sway delivers to every IPC subscriber, and the shell is one.
	#
	# --release, because a binding on the press swallows it: sway would then never deliver Super as
	# the modifier of $mod+Return, and every other shortcut on this machine would stop working.
	# --no-repeat so holding it does not open and close the menu at the key repeat rate.
	bindsym --release --no-repeat Super_L exec swaymsg -t send_tick pc:start

	# Recovery for the UI only. Sway and native applications remain running.
	bindsym --no-repeat Ctrl+Mod1+BackSpace exec /usr/local/bin/pc-shell-restart

	# A game gets the screen to itself and nothing above it.
	for_window [class="^steam_app_.*"] fullscreen enable, inhibit_idle fullscreen
	SWAY

	# The ScreenCast portal picks its backend by the DESKTOP NAME, and answers "no such capture" for
	# a name it has no backend for — which is what OBS shows as a screen capture source that lists
	# nothing to capture. sway's own session sets this, but the portal is started by systemd --user
	# and can come up before it, so it is stated here as well.
	mkdir -p /etc/xdg/xdg-desktop-portal
	printf '[preferred]\ndefault=wlr;gtk\norg.freedesktop.impl.portal.ScreenCast=wlr\norg.freedesktop.impl.portal.Screenshot=wlr\n' \
		>/etc/xdg/xdg-desktop-portal/sway-portals.conf

	# FROM THE OVERLAY IF IT IS REACHABLE, BY HAND IF IT IS NOT.
	#
	# The overlay is how an installed machine gets a newer desktop later, so an install that came
	# from it is an install that can be UPDATED — `emerge -u app-misc/posterchan-desktop` instead of
	# somebody re-running an installer. The manual path below stays as the fallback, because a first
	# install is exactly when the overlay might not be reachable yet: no network, a mirror being
	# rebuilt, a machine being provisioned before the repo was published.
	#
	# Success is checked by looking for the FILES, not by trusting emerge's exit code — a package
	# that installs nothing useful exits 0.
	if [ -f "${TARGET}/etc/portage/repos.conf/posterchan.conf" ]; then
		echo -e "\033[1;33mSyncing the PosterChanOS overlay\033[0m"
		if _in 'emerge --sync posterchan' >/dev/null 2>&1; then
			_in 'emerge -uDN --autounmask-write app-misc/posterchanos-shell' >/dev/null 2>&1
			_in 'etc-update -q --automode -5' >/dev/null 2>&1
			_in 'emerge -uDN app-misc/posterchanos-shell' >/dev/null 2>&1 || true
		else
			echo -e "\033[1;33m  the overlay is not reachable — installing the shell directly\033[0m"
		fi
		if [ -x "${TARGET}/usr/local/bin/posterchan" ] && [ -d "${TARGET}/opt/posterchan" ]; then
			echo -e "\033[1;32m  ✓ installed from the overlay\033[0m"
			# The package being present does not mean the session account was configured. In particular,
			# an install copied from a LiveCD still autologs `live` and has no installed-user profile.
			_configure_shell_session
			return 0
		fi
	fi

	# THE SHELL ITSELF. sway's config execs `posterchan`, and nothing else here installs it — so
	# without this the machine boots into an empty compositor with no way to do anything, which is
	# the most convincing possible imitation of a broken install.
	#
	# A PLAIN TARBALL, AND THE APPIMAGE ONLY IF THERE IS NOT ONE.
	#
	# The desktop ships in two shapes from one build. The AppImage is for an ordinary Linux desktop,
	# where it auto-updates and its self-contained-ness is the point. It is the WRONG shape here:
	# mounting one needs FUSE, which a minimal profile does not have, and it verifies itself on every
	# start -- so this always had to EXTRACT it, downloading a self-mounting archive purely to unpack
	# it and throw the wrapper away, while inheriting its failure modes ("VERIFY FAILED" on a machine
	# that only ever wanted the files inside).
	#
	# `PosterChan-<version>-linux-x64.tar.zst` is the same files, packed before the image is built.
	# No FUSE, no runtime, nothing to verify itself; it unpacks with the zstd this installer already
	# needs for the live image's squashfs.
	#
	# The AppImage path stays as a FALLBACK, not as a preference: a release cut before the tarball
	# existed has only the image, and an installer that refused it would fail on the last release
	# rather than the next one.
	echo -e "\033[1;33mInstalling the PosterChan desktop\033[0m"
	GH="https://github.com/loblawbob873-svg/posterchanai/releases/download/desktop-latest"
	APPTAR="/tmp/PosterChan-linux-x64.tar.zst"
	APPIMG="/tmp/PosterChan.AppImage"
	mkdir -p ${TARGET}/tmp 2>/dev/null
	# The release names the tarball with its version, which this cannot know in advance; the API
	# lists the assets, and one grep finds it without hardcoding a build number.
	if [ ! -s "$APPTAR" ]; then
		TARURL="$(curl -sSfL --retry 2 --connect-timeout 20 \
			https://api.github.com/repos/loblawbob873-svg/posterchanai/releases/tags/desktop-latest \
			2>/dev/null | grep -o 'https://[^"]*linux-x64\.tar\.zst' | head -1)"
		[ -n "$TARURL" ] && curl -sSfL --retry 3 --connect-timeout 20 -o "$APPTAR" "$TARURL" || true
	fi
	if [ ! -s "$APPTAR" ] && [ ! -f "$APPIMG" ]; then
		curl -sSfL --retry 3 --connect-timeout 20 -o "$APPIMG" "$GH/PosterChan.AppImage" \
			|| curl -sSfL --retry 2 -o "$APPIMG" https://poster.place/desktop/PosterChan.AppImage || true
	fi
	# RUN IT WHERE THE FILES ARE. This function is called BOTH ways — from the installer on the live
	# system with TARGET pointing at the new root, and from inside the chroot during finalize, where
	# TARGET is empty and the new root is simply `/`. A bare `chroot $TARGET` is a broken command in
	# the second case, and one that would have failed silently at the end of an hour-long install.
	if [ -z "$TARGET" ] || [ "$TARGET" = "/" ]; then
		_in() { /bin/bash -c "$1"; }
	else
		_in() { chroot "$TARGET" /bin/bash -c "$1"; }
	fi
	if [ -s "$APPTAR" ]; then
		[ "${TARGET:-/}" = "/" ] || cp -f "$APPTAR" ${TARGET}/tmp/PosterChan-linux-x64.tar.zst
		# Unpacked into a NEW directory and moved into place, so a half-written tree is never what
		# sway execs. `mkdir -p /opt` first: a stage3 does not guarantee it.
		_in 'mkdir -p /opt && cd /opt && rm -rf posterchan posterchan.new \
			&& mkdir -p posterchan.new \
			&& tar -C posterchan.new -xaf /tmp/PosterChan-linux-x64.tar.zst \
			&& mv posterchan.new posterchan \
			&& chmod -R a+rX /opt/posterchan \
			&& chown root:root /opt/posterchan/chrome-sandbox \
			&& chmod 4755 /opt/posterchan/chrome-sandbox \
			&& rm -f /tmp/PosterChan-linux-x64.tar.zst'
	elif [ -s "$APPIMG" ]; then
		[ "${TARGET:-/}" = "/" ] || cp -f "$APPIMG" ${TARGET}/tmp/PosterChan.AppImage
		# `mkdir -p /opt` first: a stage3 does not guarantee it, and `cd` into a directory that is not
		# there fails the whole && chain — which showed up as "the AppImage did not extract" about an
		# extraction that had never been attempted.
		_in 'mkdir -p /opt && cd /opt && rm -rf posterchan squashfs-root \
			&& chmod +x /tmp/PosterChan.AppImage \
			&& /tmp/PosterChan.AppImage --appimage-extract >/dev/null 2>&1 \
			&& mv squashfs-root posterchan \
			&& chmod -R a+rX /opt/posterchan \
			&& chown root:root /opt/posterchan/chrome-sandbox \
			&& chmod 4755 /opt/posterchan/chrome-sandbox \
			&& rm -f /tmp/PosterChan.AppImage'
	fi
	if [ -s "$APPTAR" ] || [ -s "$APPIMG" ]; then
		# A WRAPPER, NOT A SYMLINK. `AppRun` finds the binary through $APPDIR, and $APPDIR is set by
		# the AppImage RUNTIME — which is exactly the thing extracting removes. Symlinked into
		# /usr/local/bin it resolves to an empty string and the shell dies with
		# "/posterchan-desktop: No such file or directory", pointing at a path that was never real.
		#
		# chrome-sandbox above is the other half: Electron refuses to start unless it is setuid
		# root, and extraction cannot preserve a bit the archive was not allowed to carry. The
		# alternative is --no-sandbox, which turns off the renderer sandbox on a machine strangers
		# log into — not a trade worth making to save one chmod.
		# `rm -f` FIRST. /usr/local/bin/posterchan may already be a SYMLINK to AppRun from an earlier
		# install, and writing to a symlink writes THROUGH it — which replaced AppRun with a script
		# that execs itself, an infinite loop of /bin/sh processes and no window. Redirection follows
		# symlinks; only unlinking does not.
		_in 'rm -f /usr/local/bin/posterchan'
		# WHICHEVER SHAPE IS INSTALLED. A tarball install has `posterchan-desktop` and NO AppRun --
		# AppRun is an AppImage artefact that electron-builder writes when it wraps the build, and
		# the tarball is that build BEFORE the wrapping. An AppImage extraction has both, and there
		# AppRun is the shim that needs $APPDIR. Preferring the binary makes the tarball path
		# independent of a file the AppImage runtime invented.
		_in 'printf "%s\n" "#!/bin/sh" \
			"# A tarball install has the binary; an extracted AppImage has AppRun, which needs APPDIR." \
			"export APPDIR=/opt/posterchan" \
			"if [ -x \"\$APPDIR/posterchan-desktop\" ]; then exec \"\$APPDIR/posterchan-desktop\" \"\$@\"; fi" \
			"exec \"\$APPDIR/AppRun\" \"\$@\"" > /usr/local/bin/posterchan \
			&& chmod 0755 /usr/local/bin/posterchan'
		# READABLE BY THE PEOPLE WHO HAVE TO RUN IT. `--appimage-extract` inherits the umask of
		# whatever shell ran it, and an install running as root under a 0077 umask produces
		# /opt/posterchan at mode 0700 — root-only, on the one directory every session must exec
		# from. Measured on the first real boot: "Permission denied" from the shell the compositor
		# was configured to start, with the binary sitting there perfectly intact.
		if [ -e "${TARGET}/usr/local/bin/posterchan" ]; then
			echo -e "\033[1;32m  ✓ /usr/local/bin/posterchan\033[0m"
		else
			echo -e "\033[1;31m  ✗ the desktop did not unpack — sway will start with no shell\033[0m"
		fi
	else
		echo -e "\033[1;31m  ✗ could not download the PosterChan desktop — sway will start with no shell\033[0m"
	fi

	# ANYONE MAY SIGN IN, so an account has to exist before they have anywhere to put anything.
	# PosterChanOS logs in with a KEY; home directories and permissions are a Unix idea, and this is
	# what joins the two. It is the ONLY privileged thing the shell asks for, and it is limited to
	# exactly that one command — signing in with a key is not the same as being trusted with root,
	# and a machine anyone may log into must not hand every visitor sudo.
	for helper in pc-provision-user pc-session-switch pc-shell-start pc-shell-restart pc-window-cycle pc-key pc-idle update-posterchan; do
		if [ -f "$PCOS_TREE/bin/$helper" ]; then
			cp -f "$PCOS_TREE/bin/$helper" ${TARGET}/usr/local/bin/$helper
		elif [ -f /tmp/bin/$helper ]; then
			cp -f /tmp/bin/$helper ${TARGET}/usr/local/bin/$helper
		fi
		[ -f "${TARGET}/usr/local/bin/$helper" ] && chmod 0755 ${TARGET}/usr/local/bin/$helper
	done
	[ -f "${TARGET}/usr/local/bin/pc-shell-start" ] || \
		echo -e "\033[1;31m  ✗ pc-shell-start not shipped — the desktop will not be full screen\033[0m"
	if [ -f "${TARGET}/usr/local/bin/pc-provision-user" ]; then
		chmod 0755 ${TARGET}/usr/local/bin/pc-provision-user
		mkdir -p ${TARGET}/etc/sudoers.d
		printf '%s\n' \
			"# The shell provisions a Unix account for whoever signs in. This one command, nothing else." \
			"$SHELL_USER ALL=(root) NOPASSWD: /usr/local/bin/pc-provision-user" \
			> ${TARGET}/etc/sudoers.d/posterchan-provision
		chmod 0440 ${TARGET}/etc/sudoers.d/posterchan-provision
	else
		echo -e "\033[1;31m  ✗ pc-provision-user not shipped — nobody can be given an account\033[0m"
	fi
	if [ -f "${TARGET}/usr/local/bin/pc-session-switch" ]; then
		chmod 0755 ${TARGET}/usr/local/bin/pc-session-switch
		printf '%s\n' \
			"%posterchan ALL=(root) NOPASSWD: /usr/local/bin/pc-session-switch *" \
			> ${TARGET}/etc/sudoers.d/posterchan-session-switch
		chmod 0440 ${TARGET}/etc/sudoers.d/posterchan-session-switch
	fi

	# Autologin straight into the shell. A display manager is another package, another theme and
	# another thing between the power button and the desktop.
	_configure_shell_session
}

installSteam() {
	# Native Steam, explicitly. Its 32-bit graphics stack is substantial on Gentoo, but it belongs to
	# the machine rather than a Flatpak runtime and that is the PosterChanOS policy.
	# Do not patch systemd or replace Gentoo's os-release metadata to install a game launcher.
	rm -f /etc/portage/patches/sys-apps/systemd/010-posterchanos-sbat-url.patch
	# Steam/Proton still needs the real 32-bit graphics stack. A no-multilib profile cannot be made
	# safe by appending ABI_X86 after the fact; stop with the actual reason instead of emerging a
	# launcher whose games fail later. On a multilib profile, make both the loader and Mesa Vulkan
	# support explicit rather than depending on today's Steam ebuild dependency choices.
	if eselect profile show 2>/dev/null | grep -qi 'no-multilib'; then
		echo "Steam requires a multilib Gentoo profile; this system is no-multilib."
		return 1
	fi
	grep -Eq '^ABI_X86=.*[[:space:]\"]32([[:space:]\"]|$)' /etc/portage/make.conf 2>/dev/null || \
		echo 'ABI_X86="64 32"' >>/etc/portage/make.conf
	mkdir -p /etc/portage/package.use
	printf '%s\n' 'media-libs/mesa vulkan' > /etc/portage/package.use/posterchan-steam
	mkdir -p /etc/portage/package.license
	echo 'games-util/steam-launcher steam' >/etc/portage/package.license/posterchan-steam
	emerge --autounmask-write games-util/steam-launcher gui-wm/gamescope media-libs/vulkan-loader dev-util/vulkan-tools || true
	etc-update -q --automode -5
	emerge games-util/steam-launcher gui-wm/gamescope media-libs/vulkan-loader dev-util/vulkan-tools
}

locale() {
	echo "ln -sf /usr/share/zoneinfo/US/Mountain /etc/localtime" >>$TARGET/setup.sh
	echo "hwclock --systohc" >>$TARGET/setup.sh
	echo "en_US.UTF-8 UTF-8" >$TARGET/etc/locale.gen
	echo "locale-gen" >>$TARGET/setup.sh
}

fstab() {
	mkdir $TARGET/etc
	echo "UUID=$(/sbin/blkid -s UUID -o value $EFI)  /boot vfat defaults,fmask=0077,dmask=0077 0 1" >$TARGET/etc/fstab
	echo "UUID=$(/sbin/blkid -s UUID -o value $ROOT_MAPPER_NAME) / btrfs noatime,nodiratime,autodefrag,$COMPRESSION,subvol=@$ROOT_NAME 0 1" >>$TARGET/etc/fstab
	echo "UUID=$(/sbin/blkid -s UUID -o value $ROOT_MAPPER_NAME) /.snapshots btrfs noatime,nodiratime,autodefrag,$COMPRESSION,subvol=@.snapshots 0 1" >>$TARGET/etc/fstab
	echo "UUID=$(/sbin/blkid -s UUID -o value $ROOT_MAPPER_NAME) /var/lib/libvirt btrfs noatime,nodiratime,autodefrag,$COMPRESSION,subvol=@libvirt 0 1" >>$TARGET/etc/fstab
	echo "tmpfs /tmp tmpfs defaults,size=32G 0 0" >>$TARGET/etc/fstab
	echo "tmpfs /var/tmp tmpfs defaults,size=32G,mode=1777 0 0" >>$TARGET/etc/fstab
	echo "tmpfs /var/lib/systemd/coredump tmpfs defaults,size=5G 0 0" >>$TARGET/etc/fstab
	echo "UUID=$(/sbin/blkid -s UUID -o value $ROOT_MAPPER_NAME) /home btrfs noatime,nodiratime,autodefrag,$COMPRESSION,subvol=@home 0 1" >>$TARGET/etc/fstab
	echo "UUID=$(/sbin/blkid -s UUID -o value $ROOT_MAPPER_NAME) /root btrfs noatime,nodiratime,autodefrag,$COMPRESSION,subvol=@root 0 1" >>$TARGET/etc/fstab
	echo "UUID=$(/sbin/blkid -s UUID -o value $ROOT_MAPPER_NAME) /swap btrfs subvol=@swap 0 1" >>$TARGET/etc/fstab
	echo "/swap/swap none swap defaults 0 0" >>$TARGET/etc/fstab
}

accounts() {
	# Binpkgs install files as nobody:nogroup. A restored live image can retain the nobody passwd
	# entry while losing the matching group; then every binary merge fails with “Failed to find group
	# nogroup”. Recreate the conventional overflow group before any desktop packages are installed.
	if ! getent group nogroup >/dev/null 2>&1; then
		OVERFLOW_GROUP="$(getent group 65534 | cut -d: -f1)"
		if [ "$OVERFLOW_GROUP" = nobody ]; then
			# Some stage images call the standard 65534 group `nobody`; Portage binpkgs name the same
			# group `nogroup`. Rename it—the nobody user refers to the gid, so its ownership is intact.
			groupmod -n nogroup nobody || return 1
		elif [ -z "$OVERFLOW_GROUP" ]; then
			groupadd -g 65534 nogroup || return 1
		else
			echo -e "\033[1;31mGID 65534 belongs to $OVERFLOW_GROUP; refusing to rewrite it.\033[0m"
			return 1
		fi
	fi
	# ── PosterChanOS ─────────────────────────────────────────────────────────────────────────────
	# NOBODY IS NAMED IN THE IMAGE. Accounts are made when somebody signs in with a key
	# (pc-provision-user), so baking one person's login into the installer is wrong twice over: it
	# is not their machine, and it is the account every copy of the image would share.
	#
	# What still has to exist is a session to run the shell in BEFORE anyone has signed in — the
	# thing that draws the login screen. That is `posterchan`: unprivileged, no password (it is
	# reached by autologin and by nothing else), and allowed exactly one command through sudo.
	#
	# AND ROOT KEEPS A PASSWORD. The default path locks it (`passwd -dl root`), which is defensible
	# when one named human has NOPASSWD sudo — and catastrophic here, where nobody does. Measured
	# the hard way: sudo refused a sudoers file it had been handed at the wrong mode, root was
	# locked, and the only way back into a freshly installed machine was editing the kernel command
	# line at the boot menu. A recovery path is not a weakness when the alternative is a brick.
	SHELL_USER="posterchan"
	echo -e "\033[1;33mCreating the shell session account: $SHELL_USER\033[0m"
	id -u "$SHELL_USER" >/dev/null 2>&1 || \
		useradd -m -d /home/$SHELL_USER -s /bin/bash -c "PosterChan shell" $SHELL_USER
	# No password: this account is entered by autologin and must not be a way IN from anywhere
	# else — not ssh, not a login prompt, not su.
	passwd -l $SHELL_USER >/dev/null 2>&1
	# The greeter normally hands the session to a per-npub account, but it is also the live image's
	# desktop user. Give that live/repair session KVM access too, so the VM app does not change from
	# “works installed” to “permission denied” merely because PosterChanOS is being tried from USB.
	for g in audio video input netdev render kvm i2c; do
		getent group "$g" >/dev/null 2>&1 && gpasswd -a $SHELL_USER "$g" >/dev/null 2>&1
	done
	# One command, not ALL. Broad local administration is added only by finalizeInstall() on a
	# machine being installed to disk; the reusable shell/session setup stays restricted.
	mkdir -p /etc/sudoers.d
	printf '%s\n' \
		"# The shell provisions a Unix account for whoever signs in. This one command, nothing else." \
		"$SHELL_USER ALL=(root) NOPASSWD: /usr/local/bin/pc-provision-user" \
		> /etc/sudoers.d/posterchan-provision
	chmod 0440 /etc/sudoers.d/posterchan-provision
	echo "root:$ROOT_PASSWORD" | chpasswd
	grep -q '^@includedir /etc/sudoers.d' /etc/sudoers 2>/dev/null || \
		echo "@includedir /etc/sudoers.d" >>/etc/sudoers
	chown root:root /etc/sudoers && chmod 0440 /etc/sudoers
	if command -v visudo >/dev/null 2>&1; then
		visudo -c >/dev/null 2>&1 || echo -e "\033[1;31m  ✗ /etc/sudoers is not valid — sudo will refuse everything\033[0m"
	fi
	/usr/bin/hostnamectl set-hostname "${ROOT_NAME:-posterchanos}" 2>/dev/null
}

# THERE WERE TWO OF THESE. `btrfsTweaks` above and `btrfs-tweaks` here — same job, different lists
# (`/var/lib/docker` and `/volumes` only existed in this one, `/var/lib/postgresql` only in the
# other), and only one name is reachable from the command line, so half the paths were never
# touched by anything. Both were the naive `chattr -R +C $i`, which on a populated directory
# converts nothing and says nothing.
#
# One function now, with the union of the two lists; this name is kept as an alias because the help
# text and people's shell history both use it.
btrfs-tweaks() {
	btrfsTweaks
}

# The literals near the top of this legacy script remain defaults for its old repair subcommands;
# they must never become a fresh machine's real encryption key. The Live installer asks without
# echoing, confirms before destructive work, and uses the same secret as the emergency root login so
# a person has one recovery credential to retain. Automation may provide PC_INSTALL_PASSWORD through
# a protected environment; it is not accepted empty and is never written to /tmp/disk or the target.
readInstallPassword() {
	local kind="${1:-confirm}" first="${PC_INSTALL_PASSWORD:-}" second=""
	if [ -z "$first" ]; then
		read -r -s -p "Disk encryption and recovery password: " first
		echo
	fi
	if [ -z "$first" ]; then
		echo -e "${COLOR_YELLOW}A blank encryption password is not allowed.${COLOR_RESET}"
		return 1
	fi
	if [ "$kind" = "confirm" ] && [ -z "${PC_INSTALL_PASSWORD:-}" ]; then
		read -r -s -p "Confirm password: " second
		echo
		if [ "$first" != "$second" ]; then
			echo -e "${COLOR_YELLOW}Passwords did not match; nothing was written.${COLOR_RESET}"
			return 1
		fi
	fi
	DISK_PASSWORD="$first"
	ROOT_PASSWORD="$first"
	return 0
}

prepareInstallDisk() {
	local disk="/dev/$HARD_DISK"
	[ -b "$disk" ] || { echo "Not a whole disk: $disk"; return 1; }
	wipefs -a "$disk" >/dev/null || return 1
	parted -s "$disk" mklabel gpt || return 1
	parted -s -a optimal "$disk" mkpart primary fat32 1MiB 2024MiB || return 1
	parted -s -a optimal "$disk" set 1 esp on || return 1
	parted -s -a optimal "$disk" mkpart P2 2024MiB 100% || return 1
	partprobe "$disk" || return 1
	command -v udevadm >/dev/null 2>&1 && udevadm settle || true
	local n
	for n in $(seq 1 50); do
		partitionDetection
		[ -b "$EFI" ] && [ -b "$BTRFS" ] && break
		sleep .1
	done
	if [ -z "$EFI" ] || [ ! -b "$EFI" ] || [ -z "$BTRFS" ] || [ ! -b "$BTRFS" ]; then
		echo -e "\033[1;31mThe new EFI/root partitions did not appear; refusing to continue.\033[0m"
		return 1
	fi
	printf '%s' "$DISK_PASSWORD" | cryptsetup luksFormat --batch-mode --key-file=- "$BTRFS" || return 1
	# The mapper name is derived from the LUKS UUID, which did not exist before luksFormat.
	partitionDetection
	local mapper="${ROOT_MAPPER_NAME#/dev/mapper/}"
	[ -n "$mapper" ] || return 1
	printf '%s' "$DISK_PASSWORD" | cryptsetup open --key-file=- "$BTRFS" "$mapper" || return 1
	mkfs.btrfs -f "$ROOT_MAPPER_NAME" || { cryptsetup close "$mapper"; return 1; }
	mkfs.vfat -F 32 "$EFI" || { cryptsetup close "$mapper"; return 1; }
	sync
	if [ "$(blkid -s TYPE -o value "$EFI" 2>/dev/null)" != "vfat" ] \
		|| ! cryptsetup isLuks "$BTRFS" >/dev/null 2>&1; then
		cryptsetup close "$mapper" 2>/dev/null || true
		echo -e "\033[1;31mDisk verification failed after formatting; refusing to install.\033[0m"
		return 1
	fi
	cryptsetup close "$mapper" || return 1
	return 0
}

initializeDisk() {
	clear
	echo
	echo -e "\033[1;36m[PosterChanOS Installer - Initialize Device]\033[0m"
	echo
	read -p 'Proceed with Wiping the disk? (y/n): ' -i "local" choice
	if [[ $choice = *y* ]]; then
		prepareInstallDisk || return 1
		echo -e "\033[1;33mInitialize Complete. The disk was verified and is ready to install.\033[0m"
		echo
		rm -f /tmp/disk
	fi
}

wifi() {
	iwctl --passphrase $WIRELESS_PASSWORD station $WIRELESS_INTERFACE connect $SSID
}

show-help() {
	clear
	echo
	echo -e "\033[1;92m◆ gentoo.sh arguments ◆\033[0m"
	echo
	echo -e "\033[1;33m./gentoo.sh wifi\033[0m"
	echo -e "\033[1;36m[./gentoo.sh bootloader [disk] [ROOT_NAME] [ROOT_MAPPER_NAME]\033[0m"
	echo -e "\033[1;33m./gentoo.sh initialize\033[0m"
	echo -e "\033[1;36m[./gentoo.sh tar [device name] [location]\033[0m"
	echo -e "\033[1;33m./gentoo.sh snapshot\033[0m"
	echo -e "\033[1;33m./gentoo.sh reomve-snapshot\033[0m"
	echo -e "\033[1;33m./gentoo.sh btrfs-tweaks\033[0m"
	echo
}

tweaks() {
	clear
	echo
	echo -e "\033[1;36m[PosterChanOS Installer - Tools and Tweaks]\033[0m"
	echo
	echo -e "\033[1;36m[1] Chroot into existing OS\033[0m"
	echo -e "\033[1;36m[2] Enable/Disable Disk Password at Boot\033[0m"
	echo -e "\033[1;36m[3] Compile the Kernel\033[0m"
	echo -e "\033[1;36m[4] Upgrade gentoo.sh\033[0m"
	echo -e "\033[1;36m[5] Fix Audio\033[0m"
	# DELIBERATELY NOT [6], AND THE GAP IS THE POINT. The main menu's [6] is Backup/Restore Live OS,
	# which clones a running system between a disk and a USB and has been there since the first
	# commit. Adding a second [6] that also moves an operating system around — one menu away — sent
	# somebody reaching for the clone tool straight into the ISO builder, and left them sure the
	# clone tool had been deleted. It had not. Numbering this 7 and leaving 6 empty here costs a gap
	# in a list and buys "option 6" meaning exactly one thing in this script.
	echo -e "\033[1;36m[7] Build an installable ISO of this system\033[0m"
	echo -e "\033[0;90m    (to clone this system to or from a USB, use [6] on the main menu)\033[0m"
	echo -e "\033[1;36m[8] Change the disk encryption password\033[0m"
	echo
	read -p 'Your Choice: ' choice
	if [[ $choice = 1 ]]; then
		setDevices
		systemMounts
		/usr/bin/chroot $TARGET /bin/bash
	elif [[ $choice = 2 ]]; then
		clear
		echo -e "\033[1;36m[Password Protection at Boot]\033[0m"
		echo
		echo
		partitionDetection
		read -p 'Unlock Disk without password at boot time? ' -e -i "y" pass_change
		if [[ $pass_change = *n* ]]; then
			AUTO_DECRYPT="False"
			bootloader
		else
			AUTO_DECRYPT="True"
			bootloader
		fi
	elif [[ $choice = 3 ]]; then
		compile-kernel
	elif [[ $choice = 4 ]]; then
		upgradeSelf
	elif [[ $choice = 5 ]]; then
		fixSound
	elif [[ $choice = 7 ]]; then
		liveCD
		tweaks
	elif [[ $choice = 8 ]]; then
		changeDiskPassword
		tweaks
	else
		tweaks
	fi
}

# ===============================================================================================
# CHANGE THE DISK ENCRYPTION PASSWORD.
#
# `cryptsetup luksChangeKey`, on the slot the old password opens, and nothing else — no reformat, no
# re-encrypt, no reboot. The data is encrypted with a master key that never changes; a LUKS password
# only unlocks that key, so changing one is a header write of a few kilobytes and is instant even on
# a full disk.
#
# THREE THINGS ARE VERIFIED BEFORE ANYTHING IS WRITTEN, because the failure here is not an error
# message, it is a machine that will not boot:
#
#   1. THE DEVICE IS ACTUALLY LUKS. `partitionDetection` reads /etc/disk and picks the second
#      partition; on a machine partitioned differently that is somebody's data. `isLuks` refuses
#      rather than writing a header over it.
#   2. THE OLD PASSWORD REALLY OPENS IT, tested with `luksOpen --test-passphrase` first. Handing a
#      wrong one to luksChangeKey fails harmlessly, but asking first means the person is told which
#      password was wrong instead of reading a cryptsetup exit code.
#   3. THE NEW ONE IS TYPED TWICE and is not empty. There is no "forgot password" for this.
#
# AND THE KEYFILE IS THE PART THAT IS EASY TO FORGET. If this install unlocks itself at boot, there
# is a SECOND key in another slot — /boot/keyfile.key, added by setLuksKeyfile — and it is untouched
# by a password change, which is correct: the machine goes on booting hands-free and the typed
# password changes. It is said out loud, because "I changed my disk password and it still boots
# without asking" is otherwise a very reasonable thing to be alarmed by.
# ===============================================================================================
changeDiskPassword() {
	clear
	echo
	echo -e "${COLOR_CYAN}═══════════════════════════════════════════════════════${COLOR_RESET}"
	echo -e "${COLOR_BOLD}  ⚡ CHANGE THE DISK ENCRYPTION PASSWORD ⚡${COLOR_RESET}"
	echo -e "${COLOR_CYAN}═══════════════════════════════════════════════════════${COLOR_RESET}"
	echo

	if [[ $EUID -ne 0 ]]; then
		echo -e "${COLOR_YELLOW}This has to run as root — it writes the LUKS header.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi
	if ! command -v cryptsetup >/dev/null 2>&1; then
		echo -e "${COLOR_YELLOW}cryptsetup is not installed on this system.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi

	partitionDetection

	local DEV
	DEV="$BTRFS"
	read -p 'Encrypted device: ' -e -i "$DEV" DEV
	if [[ -z "$DEV" || ! -b "$DEV" ]]; then
		echo -e "${COLOR_YELLOW}$DEV is not a block device — nothing was changed.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi
	# THE ONE CHECK THAT STOPS THIS DESTROYING A DISK. Detection guesses at the layout; this asks.
	if ! cryptsetup isLuks "$DEV" 2>/dev/null; then
		echo -e "${COLOR_YELLOW}$DEV is not a LUKS volume. Nothing was changed.${COLOR_RESET}"
		echo -e "${COLOR_YELLOW}(Detection guesses the layout from /etc/disk; this machine's may differ.)${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi

	echo
	cryptsetup luksDump "$DEV" 2>/dev/null | grep -iE '^Version|^[[:space:]]*[0-9]+: luks|Key Slot' | head -12
	echo

	# -s: never echoed, and never left in the environment or a file. `read -s` keeps it in a shell
	# variable that dies with the function.
	local OLD NEW1 NEW2
	read -rsp 'Current disk password: ' OLD; echo
	if [[ -z "$OLD" ]]; then
		echo -e "${COLOR_YELLOW}Nothing entered — nothing was changed.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi
	# ASKED BEFORE IT MATTERS. --test-passphrase opens nothing and writes nothing; it just answers
	# whether this password has a slot, so a typo is reported as a typo.
	if ! printf '%s' "$OLD" | cryptsetup luksOpen --test-passphrase "$DEV" - >/dev/null 2>&1; then
		echo -e "${COLOR_YELLOW}That password does not open $DEV. Nothing was changed.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi

	read -rsp 'New disk password: ' NEW1; echo
	read -rsp 'New disk password again: ' NEW2; echo
	if [[ -z "$NEW1" ]]; then
		echo -e "${COLOR_YELLOW}An empty password is not a password — nothing was changed.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi
	if [[ "$NEW1" != "$NEW2" ]]; then
		echo -e "${COLOR_YELLOW}The two did not match — nothing was changed.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi

	echo
	echo -e "${COLOR_YELLOW}Writing the new key…${COLOR_RESET}"
	# BOTH ON STDIN, old then new, which is what luksChangeKey reads when no key file is given. It
	# replaces the slot the old password occupied rather than adding a second one, so the old
	# password stops working the moment this returns.
	if printf '%s\n%s' "$OLD" "$NEW1" | cryptsetup luksChangeKey "$DEV" 2>&1; then
		echo
		echo -e "${COLOR_GREEN}◆ DONE ◆${COLOR_RESET}"
		echo -e "    The disk password for $DEV has been changed."
		# The hands-free unlock, if this install has one. Untouched on purpose, and said out loud.
		if [[ -f /boot/keyfile.key ]] || grep -q 'keyfile' /etc/crypttab 2>/dev/null; then
			echo
			echo -e "${COLOR_YELLOW}This machine also unlocks itself at boot with /boot/keyfile.key,${COLOR_RESET}"
			echo -e "${COLOR_YELLOW}which is a separate key slot and is unchanged — so it will still${COLOR_RESET}"
			echo -e "${COLOR_YELLOW}boot without asking. Use [2] above to turn that off.${COLOR_RESET}"
		fi
		echo
		echo -e "${COLOR_YELLOW}Test it before you reboot: open another terminal and run${COLOR_RESET}"
		echo -e "    cryptsetup luksOpen --test-passphrase $DEV"
	else
		echo
		echo -e "${COLOR_YELLOW}cryptsetup refused the change. The old password still works.${COLOR_RESET}"
	fi
	echo
	read -p "Press enter key to Continue"
}

# ===============================================================================================
# TURN THE RUNNING SYSTEM INTO A LIVE CD.
#
# Not an installer image and not a backup: a bootable ISO of THIS machine, exactly as it is now,
# that boots to the same desktop on any other machine and writes nothing to its disk.
#
# HOW IT WORKS, because the three pieces have to agree or it boots to a dracut shell:
#
#   1. mksquashfs packs `/` into LiveOS/squashfs.img on the ISO;
#   2. a FRESH dracut initramfs is built with `dmsquash-live` and `--no-hostonly` — the installed
#      one is deliberately host-only and unlocks THIS machine's LUKS volume, which is precisely
#      wrong on somebody else's hardware;
#   3. grub-mkrescue wraps it in a hybrid ISO that boots on BIOS and UEFI alike.
#
# THE SWAPFILE IS EXCLUDED, AND SO IS ITS FSTAB LINE. Those are two separate things and leaving
# either one in is its own failure. The FILE is gigabytes of nothing, which is the difference
# between an ISO that fits on a stick and one that does not. The fstab ENTRY is worse: the live
# system would try to swapon a file that is not there, and on a systemd box a failed swap unit at
# boot is a delay and a red line on a machine whose whole job is to boot cleanly for a stranger.
# `/etc/fstab` is rewritten inside the image with mksquashfs's pseudo-file feature, so nothing on
# the running system is touched to do it.
#
# THE WORK DIRECTORY EXCLUDES ITSELF. Squashing `/` while writing the squashfs into `/` is a loop
# that fills the disk, and it is the first thing anyone gets wrong here.
# ===============================================================================================
# UPDATE THIS SCRIPT FROM WHERE IT IS ACTUALLY MAINTAINED.
#
# "livecd error: same as fucking last time i said it!" — and the fix had been written, committed,
# pushed and deployed twice. It never arrived, because this menu entry pulled from
# `nas.lan:~/configs/scripts/gentoo.sh` over scp: a private path, on one machine, that nothing
# updates when the repository does. So "Upgrade gentoo.sh" faithfully reinstalled the same old copy
# every time, and every fix since this script moved into the repo has been invisible to anybody who
# used it. That is worse than having no updater, because it looks like one.
#
# The public mirror is fetched instead. No ssh, no key, no one host that has to be up — and it is
# the same file the repository holds, which is the whole point.
#
# NOTHING IS REPLACED UNTIL THE DOWNLOAD IS PROVEN. A truncated or 404 body written over the running
# script is a person left with no installer at all, on a machine they may be part-way through
# installing. It lands in a temp file, is checked for size, for a shebang and for `bash -n`, and only
# then moves into place — and the old one is kept beside it either way.
# ===============================================================================================
upgradeSelf() {
	local URL TMP HERE
	URL="https://raw.githubusercontent.com/loblawbob873-svg/posterchanai/main/os/gentoo.sh"
	HERE="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null)"
	[[ -z "$HERE" ]] && HERE="$PWD/gentoo.sh"
	TMP="$(mktemp /tmp/gentoo.sh.XXXXXX)" || { echo "cannot write to /tmp"; read -p "Press enter key to Continue"; return; }
	echo
	echo -e "${COLOR_YELLOW}Fetching $URL${COLOR_RESET}"
	if ! curl -fsSL --retry 2 --max-time 60 -o "$TMP" "$URL"; then
		rm -f "$TMP"
		echo -e "${COLOR_YELLOW}Could not download it — the copy you have is untouched.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi
	# Proven before it is trusted: a 404 page and a half-written file both arrive as "success".
	if [[ ! -s "$TMP" ]] || ! head -1 "$TMP" | grep -q '^#!' || ! bash -n "$TMP" 2>/dev/null; then
		rm -f "$TMP"
		echo -e "${COLOR_YELLOW}What came back is not a working script — nothing was replaced.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi
	cp -f "$HERE" "$HERE.old" 2>/dev/null
	if ! install -m 755 "$TMP" "$HERE"; then
		rm -f "$TMP"
		echo -e "${COLOR_YELLOW}Could not write $HERE — try it as root.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi
	rm -f "$TMP"
	# THE COPY ON $PATH TOO, when there is one. `gentoo.sh` is installed to /usr/bin on these
	# machines and run by name, so updating only the file in the current directory updates the copy
	# nobody runs — which is this bug wearing a different hat.
	local ONPATH
	ONPATH="$(command -v gentoo.sh 2>/dev/null)"
	if [[ -n "$ONPATH" && "$ONPATH" != "$HERE" ]]; then
		if install -m 755 "$HERE" "$ONPATH" 2>/dev/null; then
			echo -e "${COLOR_YELLOW}Also updated $ONPATH${COLOR_RESET}"
		else
			echo -e "${COLOR_YELLOW}Could not update $ONPATH (needs root) — run it from $HERE.${COLOR_RESET}"
		fi
	fi
	rm -f repos.conf gentoobinhost.conf /tmp/latest-stage3-amd64-desktop-systemd.txt
	echo
	echo -e "${COLOR_YELLOW}Updated: $HERE${COLOR_RESET}"
	echo -e "${COLOR_YELLOW}The previous copy is at $HERE.old${COLOR_RESET}"
	echo -e "${COLOR_YELLOW}Start it again to run the new one.${COLOR_RESET}"
	read -p "Press enter key to Continue"
	exit 0
}

liveCD() {
	clear
	echo
	echo -e "${COLOR_CYAN}═══════════════════════════════════════════════════════${COLOR_RESET}"
	echo -e "${COLOR_BOLD}  ⚡ BUILD A LIVE CD FROM THIS RUNNING SYSTEM ⚡${COLOR_RESET}"
	echo -e "${COLOR_CYAN}═══════════════════════════════════════════════════════${COLOR_RESET}"
	echo

	# ---------------------------------------------------------------- the log, before anything else
	#
	# "i can't read the error generating a live cd because it goes back to the menu."
	#
	# Every failure below prints a line and waits for a keypress — and then the menu redraws with
	# `clear`, so whatever was on screen is gone the moment somebody presses the key they were just
	# told to press. On a build that takes half an hour and prints hundreds of lines, the message
	# that matters has usually scrolled away before that anyway. A transcript is the only thing that
	# survives both.
	#
	# It is opened HERE, at a fixed path, rather than under the output directory — the earliest
	# failures (a missing tool, a squashfs-tools without zstd) happen before anybody has been asked
	# where the ISO should go, and a log that only exists after that point cannot record them.
	local LOG="/var/tmp/pc-livecd.log"
	: >"$LOG" 2>/dev/null || LOG="/dev/null"
	{ echo "=== $(date) ==="; echo "host=$(uname -a)"; } >>"$LOG" 2>/dev/null
	# Every "it went back to the menu" path routes through this, so the path is on screen at the
	# moment of failure AND the reason is in a file that outlives the redraw.
	_lcd_fail() {
		echo "FAILED: $*" >>"$LOG" 2>/dev/null
		echo
		echo -e "${COLOR_YELLOW}$*${COLOR_RESET}"
		echo -e "${COLOR_YELLOW}Full details: $LOG${COLOR_RESET}"
		echo -e "${COLOR_YELLOW}(read it with:  less $LOG )${COLOR_RESET}"
		read -p "Press enter key to Continue"
	}

	if [[ $EUID -ne 0 ]]; then
		echo -e "${COLOR_YELLOW}This has to run as root — it reads every file on the disk.${COLOR_RESET}"
		read -p "Press enter key to Continue"
		return
	fi

	# ---------------------------------------------------------------- tools
	# mksquashfs packs the filesystem, xorriso (libisoburn) writes the ISO, and grub-mkrescue needs
	# mtools + dosfstools to build the little FAT image that makes it UEFI-bootable. A missing
	# mtools is the classic one: grub-mkrescue then produces an ISO that boots on BIOS and is
	# invisible to every UEFI machine, with only a warning to say so.
	local NEED=""
	command -v mksquashfs >/dev/null 2>&1 || NEED="$NEED sys-fs/squashfs-tools"
	command -v xorriso >/dev/null 2>&1 || NEED="$NEED dev-libs/libisoburn"
	command -v mformat >/dev/null 2>&1 || NEED="$NEED sys-fs/mtools"
	command -v mkfs.vfat >/dev/null 2>&1 || NEED="$NEED sys-fs/dosfstools"
	command -v dracut >/dev/null 2>&1 || NEED="$NEED sys-kernel/dracut"
	command -v grub-mkrescue >/dev/null 2>&1 || NEED="$NEED sys-boot/grub"
	command -v zstd >/dev/null 2>&1 || NEED="$NEED app-arch/zstd"
	if [[ -n "$NEED" ]]; then
		echo -e "${COLOR_YELLOW}Installing what this needs:${COLOR_RESET}$NEED"
		echo
		/usr/bin/emerge -n $NEED 2>&1 | tee -a "$LOG"
		# PIPESTATUS, NOT the pipeline's own status. `if ! cmd | tee` tests TEE, which succeeds
		# whatever happened upstream — the same trap the /logs board paid for, and it would turn
		# every failure here into a silent success that goes on to build half an ISO.
		if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
			_lcd_fail "Could not install:$NEED — stopping rather than building half an ISO."
			return
		fi
	fi

	# ---------------------------------------------------------------- can it compress?
	# "make live cd fails with zstd is not supported", after every slow step had already run.
	#
	# `-comp zstd` below is not optional — the initramfs and the kernel both read the image back
	# with it — and squashfs-tools only speaks it when it was BUILT with the zstd USE flag. Having
	# the flag in make.conf does not rebuild a copy that is already installed, so the tool sits
	# there, present and missing exactly one compressor, and says so only when it is handed the
	# whole filesystem to pack.
	#
	# Asked here, of the real binary, and repaired with --newuse rather than assumed. It is the
	# cheapest possible question and it comes before anything expensive.
	if ! _pc_mksquashfs_zstd; then
		echo -e "${COLOR_YELLOW}mksquashfs on this system cannot compress with zstd — rebuilding it${COLOR_RESET}"
		echo -e "${COLOR_YELLOW}with the flag, which is what the live image and its initramfs need.${COLOR_RESET}"
		echo
		mkdir -p /etc/portage/package.use
		echo "sys-fs/squashfs-tools zstd lzma lzo xz" >/etc/portage/package.use/livecd-squashfs
		/usr/bin/emerge -n --newuse sys-fs/squashfs-tools 2>&1 | tee -a "$LOG"
		# PIPESTATUS, NOT the pipeline's own status. `if ! cmd | tee` tests TEE, which succeeds
		# whatever happened upstream — the same trap the /logs board paid for, and it would turn
		# every failure here into a silent success that goes on to build half an ISO.
		if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
			_lcd_fail "Could not rebuild squashfs-tools with zstd — stopping."
			return
		fi
		if ! _pc_mksquashfs_zstd; then
			{ echo "post-rebuild zstd trial still fails; mksquashfs -version:";
			  mksquashfs -version 2>&1 | head -3; } >>"$LOG" 2>/dev/null
			_lcd_fail "It rebuilt and mksquashfs still cannot write a zstd image. The trial output is in the log."
			return
		fi
	fi

	# ---------------------------------------------------------------- where
	#
	# WHERE IT LANDS IS DECIDED HERE AND SAID OUT LOUD, because an answer that quietly becomes
	# somewhere else costs the whole build. "the iso is saving to ~" was that: `read -e -i` pre-fills
	# only on a real terminal, so an empty or relative answer resolved against whatever directory the
	# script was started from and a multi-gigabyte image landed there without anybody choosing it.
	#
	# The home directory is now the DEFAULT, deliberately — "make the default dir for livecd your
	# homedir" — which is a different thing from ending up there by accident. It is printed before
	# anything slow starts, with the free space beside it.
	local OUTDIR ISO WORK LABEL DEFOUT
	# THE DEFAULT IS THE HOME DIRECTORY OF WHOEVER ASKED, which is where somebody looks for a file
	# they just made. It used to be /var/tmp/livecd — a fine place for a build tree and a strange
	# place to go hunting for an ISO.
	#
	# `$HOME` is the wrong question when this runs under sudo: it is root's. `$SUDO_USER` names the
	# person who actually typed the command, and their home is what they mean by "my home directory".
	# Falls back to /var/tmp/livecd when neither can be written to, because a default that cannot be
	# used is not a default.
	DEFOUT=""
	if [[ -n "${SUDO_USER:-}" ]]; then
		DEFOUT="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)"
	fi
	[[ -z "$DEFOUT" ]] && DEFOUT="$HOME"
	if [[ -z "$DEFOUT" || ! -d "$DEFOUT" || ! -w "$DEFOUT" ]]; then
		DEFOUT="/var/tmp/livecd"
	else
		DEFOUT="${DEFOUT%/}/livecd"
	fi
	OUTDIR="${PC_ISO_OUT:-}"
	[ -n "$OUTDIR" ] || read -p 'Write the ISO where? ' -e -i "$DEFOUT" OUTDIR
	# An empty answer is the default, not the current directory. `read -e -i` pre-fills only on a
	# real terminal; anywhere else it hands back "".
	OUTDIR="${OUTDIR:-$DEFOUT}"
	OUTDIR="${OUTDIR/#\~/$HOME}"                 # ~ is not expanded by read
	if [[ "$OUTDIR" != /* ]]; then
		echo -e "${COLOR_YELLOW}'$OUTDIR' is a relative path — it would land under $PWD.${COLOR_RESET}"
		echo -e "${COLOR_YELLOW}Give a full path starting with /, or press enter for $DEFOUT.${COLOR_RESET}"
		read -p 'Write the ISO where? ' -e -i "$DEFOUT" OUTDIR
		OUTDIR="${OUTDIR:-$DEFOUT}"
		OUTDIR="${OUTDIR/#\~/$HOME}"
		[[ "$OUTDIR" != /* ]] && { echo "still not a full path — stopping."; read -p "Press enter key to Continue"; return; }
	fi
	OUTDIR="${OUTDIR%/}"
	mkdir -p "$OUTDIR" || { _lcd_fail "cannot write to $OUTDIR"; return; }
	LABEL="PCLIVE"
	ISO="$OUTDIR/posterchan-live-$(date +%Y%m%d).iso"
	WORK="$OUTDIR/work"
	# SAID BEFORE THE SLOW PART, with the room there is. The image and its work tree are several
	# gigabytes and the build discovers a full disk at the very end, after packing everything.
	local FREE
	FREE="$(df -BG --output=avail "$OUTDIR" 2>/dev/null | tail -1 | tr -dc '0-9')"
	echo
	echo -e "${COLOR_YELLOW}ISO:  $ISO${COLOR_RESET}"
	echo -e "${COLOR_YELLOW}Work: $WORK${COLOR_RESET}"
	[[ -n "$FREE" ]] && echo -e "${COLOR_YELLOW}Free here: ${FREE}G${COLOR_RESET}"
	if [[ -n "$FREE" && "$FREE" -lt 12 ]]; then
		echo -e "${COLOR_YELLOW}That is not much room for a squashfs plus an ISO of it.${COLOR_RESET}"
		local GOON
		read -p 'Carry on anyway? ' -e -i "n" GOON
		[[ "${GOON,,}" == y* ]] || { read -p "Press enter key to Continue"; return; }
	fi
	rm -rf "$WORK"
	mkdir -p "$WORK/iso/LiveOS" "$WORK/iso/boot/grub"
	# A TRANSCRIPT, because "i tried to make iso again but no work" is not something anybody can act
	# on and the build is far too long to ask somebody to sit through twice. Every step below appends
	# a line; the failure paths already print, and this is where those prints end up when the screen
	# has scrolled or the terminal has been closed.
	{ echo "iso=$ISO work=$WORK free=${FREE:-?}G kernel-to-find=$(uname -r)"; } >>"$LOG" 2>/dev/null
	echo -e "${COLOR_YELLOW}Log:  $LOG${COLOR_RESET}"

	local KEEP_HOME
	KEEP_HOME="${PC_ISO_HOME:-}"
	[ -n "$KEEP_HOME" ] || read -p 'Include /home in the image? ' -e -i "n" KEEP_HOME

	# ---------------------------------------------------------------- whose machine is this
	#
	# AN ISO OF YOUR MACHINE IS A COPY OF YOUR MACHINE, and that is fine for a rescue disc and wrong
	# for a disc you hand somebody. Left alone it carries your account, your password hash, your ssh
	# HOST keys, your saved wifi passwords and your shell history — and it autologins as you, on a
	# stranger's hardware, with your name on the screen.
	#
	# "laptop needs to reflect a new os install and not have verita84 configured at all". So the
	# default is CLEAN: every account above uid 1000 is dropped, the secrets below are left out of
	# the image entirely, and a passwordless `live` account is written in to autologin instead. The
	# installer on the disc then makes the real account on the machine it installs to.
	#
	# Answering `n` keeps everything — which is the right answer for a rescue disc of your own
	# machine, and the wrong one for anything you publish. It says so rather than assuming.
	# ANSWERABLE WITHOUT A KEYBOARD, and the DEFAULT WHEN UNANSWERED IS CLEAN.
	#
	# `read -e -i "y"` pre-fills only on a terminal. Driven from a script the pre-fill does not
	# happen, so a blank line leaves CLEAN empty -- and `[[ "$CLEAN" = *y* ]]` on an empty string is
	# FALSE, which is the personal-rescue-disc branch. An unattended build would have quietly
	# produced an image carrying this machine's accounts, ssh host keys and saved wifi passwords,
	# and nothing on screen would have said so. So the env var is read first and an EMPTY answer
	# means clean, not the opposite.
	local CLEAN
	CLEAN="${PC_ISO_CLEAN:-}"
	[ -n "$CLEAN" ] || read -p "Clean out this machine's accounts and secrets (n = personal rescue disc)? " -e -i "y" CLEAN
	[ -n "$CLEAN" ] || CLEAN=y

	# ---------------------------------------------------------------- what to leave out
	#
	# EVERY SWAPFILE, found rather than assumed. It is usually /swapfile, and on this installer it
	# is whatever hibernation() made — so the list is built from what the kernel says is in use
	# (/proc/swaps) AND from fstab, because a swapfile that is configured but not currently on is
	# still gigabytes of nothing in the image.
	local SWAPFILES=""
	if [[ -r /proc/swaps ]]; then
		SWAPFILES="$(awk 'NR>1 && $2=="file" {print $1}' /proc/swaps)"
	fi
	SWAPFILES="$SWAPFILES $(awk '!/^[[:space:]]*#/ && $3=="swap" && $1 ~ /^\// {print $1}' /etc/fstab 2>/dev/null)"

	local EXCLUDES=(
		proc sys dev run tmp mnt media lost+found
		var/tmp var/cache/distfiles var/cache/binpkgs var/lib/portage/distfiles
		# Runtime payloads are not part of an operating-system image. Keep the engines and their
		# configuration, but never pack container layers, volumes, NVRAM or multi-gigabyte VM disks.
		var/lib/docker var/lib/containers var/lib/containerd var/lib/libvirt
		var/log/journal .snapshots
		boot efi
		etc/fstab etc/machine-id etc/crypttab etc/dracut.conf.d
	)
	# PosterChanOS uses qemu:///session, so its VM disks live in each user's private home instead of
	# /var/lib/libvirt. A personal rescue image may keep the rest of /home; it must still not quietly
	# turn every qcow2 disk into part of the ISO.
	local VMHOME
	for VMHOME in /home/*/.local/share/PosterChanOS/vms; do
		[[ -e "$VMHOME" ]] && EXCLUDES+=("${VMHOME#/}")
	done
	# THE WORK DIRECTORY EXCLUDES ITSELF — see the header. Stored relative, because mksquashfs's
	# -e paths are relative to the source root.
	EXCLUDES+=("${OUTDIR#/}")
	# A CLEAN IMAGE HAS NOBODY'S HOME IN IT, whatever was answered above. The two questions can be
	# answered in contradiction — strip the accounts, keep the home directories — and honouring both
	# literally would ship somebody's files under a user that no longer exists to own them, readable
	# by uid 1000, which is `live`.
	# EXCLUDE THE HOMES INDIVIDUALLY, NOT /home ITSELF.
	#
	# CORRECTION, MEASURED: an earlier version of this comment said `-e home` suppresses the
	# pseudo-file entries written into it. That is FALSE. Run against squashfs-tools, `-e home`
	# together with `home/live d` and `home/live/.bash_profile f` produces exactly what was asked
	# for — the operator's files gone, the pseudo entries present. I asserted the opposite from a
	# symptom and shipped a fix for it; the real cause was elsewhere (see SESS_USER below).
	#
	# Naming each home is kept anyway, because it is the clearer statement of intent — "these
	# people's files are not in the image" rather than "the directory is gone and then partly put
	# back" — and it leaves /home visibly in the tree, which is what the entries below write into.
	if [[ "$KEEP_HOME" = *n* || "$CLEAN" = *y* ]]; then
		local H
		for H in /home/*; do
			[[ -e "$H" ]] && EXCLUDES+=("${H#/}")
		done
		# A dotfile directly under /home (rare, but /home/.snapshots exists on btrfs installs) is
		# not matched by the glob above.
		for H in /home/.[!.]*; do
			[[ -e "$H" ]] && EXCLUDES+=("${H#/}")
		done
	fi
	if [[ "$CLEAN" = *y* ]]; then
		# A live image made from an already-claimed machine must still be a first boot. Keeping this
		# root-owned marker makes pc:os:provisioned answer true even though every person and browser
		# profile was scrubbed, so the Welcome flow deliberately stays hidden. It also assigns the
		# first real login's administrator rights to an npub that is not present on the disc.
		EXCLUDES+=(var/lib/posterchanos etc/sudoers.d/posterchan-admin
			etc/systemd/system/boot-snapshot.service
			etc/systemd/system/boot-snapshot.timer
			etc/systemd/system/default.target.wants/boot-snapshot.timer
			etc/systemd/system/timers.target.wants/boot-snapshot.timer
			etc/systemd/system/multi-user.target.wants/boot-snapshot.service
			etc/systemd/system/multi-user.target.wants/sshd.service)
	fi
	local f
	for f in $SWAPFILES; do
		[[ -n "$f" ]] && EXCLUDES+=("${f#/}")
	done

	# NEVER FOLLOW ANOTHER MOUNT INTO THE IMAGE.  A builder may have removable media, NAS data or
	# phone-sync storage mounted at an ordinary top-level path (this host uses /usb).  mksquashfs
	# crosses filesystem boundaries by default, so a path not named in the static list above would
	# silently become part of the public ISO.  Preserve /home only when it was explicitly requested;
	# every other subordinate mount is host/external state, not part of this root filesystem.
	local MP
	while IFS= read -r MP; do
		[[ -z "$MP" || "$MP" == / ]] && continue
		[[ "$MP" == /home && "$KEEP_HOME" != *n* && "$CLEAN" != *y* ]] && continue
		EXCLUDES+=("${MP#/}")
	done < <(findmnt -rn -o TARGET 2>/dev/null | sort -u)

	echo
	echo -e "${COLOR_MAGENTA}◆ LEAVING OUT ◆${COLOR_RESET}"
	printf '    %s\n' "${EXCLUDES[@]}"
	echo

	# ---------------------------------------------------------------- the live /etc/fstab
	#
	# Written into the image, never over the real one. A live system mounts its root from the ISO,
	# so every line here would be wrong on another machine — and the swap line would fail loudly.
	# What is left is the handful of pseudo-filesystems, which cost nothing and keep anything that
	# reads fstab happy.
	local LIVEFSTAB="$WORK/fstab.live"
	cat >"$LIVEFSTAB" <<'FSTAB'
# Live image — the root filesystem comes from the ISO, so nothing is mounted from a disk here.
# The swap entry from the machine this was built on is deliberately absent: its swapfile is not in
# this image, and a swapon that cannot find its file is a failed unit on every boot.
tmpfs /tmp tmpfs defaults,nosuid,nodev 0 0
FSTAB

	# ---------------------------------------------------------------- strip the operator
	#
	# TWO MECHANISMS, BECAUSE THEY DO DIFFERENT THINGS. `-e` leaves a path OUT of the image, which is
	# how a secret stops existing; a pseudo-file REPLACES one, which is how /etc/passwd can lose an
	# account without losing root and the system users everything else depends on. Getting these the
	# wrong way round gives either an image that still holds the secret or one that will not boot.
	if [[ "$CLEAN" = *y* ]]; then
		# Host identity. ssh host keys are the sharp one: every machine installed from this ISO would
		# present the SAME host key, so they are indistinguishable to anything that checks, and a
		# stolen copy impersonates all of them.
		local F
		for F in /etc/ssh/ssh_host_*; do [[ -e "$F" ]] && EXCLUDES+=("${F#/}"); done
		# Saved networks, in each of the three places something might keep them.
		for F in /etc/NetworkManager/system-connections /var/lib/iwd /etc/wpa_supplicant; do
			[[ -e "$F" ]] && EXCLUDES+=("${F#/}")
		done
		# Root's home, the logs and the account databases' backups (passwd- and shadow- hold exactly
		# what the rewritten ones are dropping, which is a good way to undo this work by accident).
		for F in /root /var/log /etc/passwd- /etc/shadow- /etc/group- /etc/gshadow-; do
			[[ -e "$F" ]] && EXCLUDES+=("${F#/}")
		done

		# A release image is an operating system, not a backup of services that happened to run on
		# the build host.  These trees contain databases, private application state, container layers,
		# relay/media data and (in several cases) credentials.  They are both enormous and unsafe to
		# publish.  Keep Portage/system state under /var, but remove known mutable service payloads.
		for F in /var/www /var/intel \
			/var/lib/gitea /var/lib/postgresql \
			/var/lib/posterchanai /var/lib/synapse /var/lib/pleroma \
			/var/lib/redis /var/lib/radicale /var/lib/tor /var/lib/letsencrypt; do
			[[ -e "$F" ]] && EXCLUDES+=("${F#/}")
		done

		# /opt is commonly where a builder accumulates SDKs and unrelated server applications.  The
		# one payload a PosterChanOS image needs is /opt/posterchan; exclude every sibling explicitly
		# so that directory remains available to the image self-check below.
		for F in /opt/*; do
			[[ -e "$F" && "$F" != /opt/posterchan ]] && EXCLUDES+=("${F#/}")
		done

		# The account files, rewritten. Everything below uid 1000 stays — root and the system users
		# are what makes a Linux system work — and every real person is dropped, replaced by one
	# console-only, password-locked `live`.
		awk -F: '$3 < 1000 || $3 >= 65534' /etc/passwd  >"$WORK/passwd"
		echo 'live:x:1000:1000:Live session:/home/live:/bin/bash' >>"$WORK/passwd"
		awk -F: '$3 < 1000 || $3 >= 65534' /etc/group   >"$WORK/group"
		echo 'live:x:1000:' >>"$WORK/group"
		# LOCKED password field. agetty's console autologin invokes login's preauthenticated (`-f`)
		# path as root, so it does not need a password hash. `!` prevents this disposable account from
		# being used through ssh, a display-manager password prompt, or any other authentication path.
		awk -F: 'NR==FNR { if ($3 >= 1000 && $3 < 65534) drop[$1]; next } !($1 in drop)' \
			/etc/passwd /etc/shadow >"$WORK/shadow" 2>/dev/null || cp /etc/shadow "$WORK/shadow"
		# A CLEAN DISC MUST NOT CARRY THE BUILD MACHINE'S ROOT PASSWORD HASH. The live account has
		# its narrowly-scoped NOPASSWD rule below, so direct root login is unnecessary; lock it in the
		# image while leaving the running machine untouched.
		sed -i 's/^root:[^:]*/root:!/' "$WORK/shadow"
		echo 'live:!:20000:0:99999:7:::' >>"$WORK/shadow"
		# The groups that decide whether a desktop can use the hardware. Taken from what THIS machine
		# actually has rather than a guessed list, because a live user outside `video`/`input` gets a
		# desktop with no screen and no keyboard.
		local G
		for G in wheel video input audio render seat plugdev users; do
			grep -q "^$G:" /etc/group && sed -i "s/^\($G:[^:]*:[^:]*:\)\(.*\)$/\1\2,live/; s/,live,live/,live/; s/:,live$/:live/" "$WORK/group"
		done
		# AND IT CAN BECOME ROOT WITHOUT ONE.
		#
		# `live` is password-LOCKED, so the installer needs an explicit sudo rule. The disc's whole
		# purpose is installing, which is root's job; the local console is already physical access to
		# the install media, while password and ssh login to this account remain impossible.
		#
		# NOPASSWD for the live account only, in its own drop-in. This is what every live image does
		# and it is safe for the same reason theirs is: the medium is read-only, the session is
		# transient, and anybody holding the disc can already read every file on it. It does NOT
		# touch the RUNNING host's /etc/sudoers. The image gets a clean main file below, including the
		# @includedir that makes this drop-in real; the source host used for the failed disc omitted
		# that directive, so the perfectly formed live rule was silently unreachable.
		mkdir -p "$WORK/sudoers.d"
		printf 'live ALL=(ALL:ALL) NOPASSWD: ALL\n' >"$WORK/sudoers.d/live"
		cat >"$WORK/sudoers" <<-'SUDOERS'
		Defaults env_reset
		Defaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
		root ALL=(ALL:ALL) ALL
		@includedir /etc/sudoers.d
		SUDOERS
		chmod 0440 "$WORK/sudoers" "$WORK/sudoers.d/live"
		if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$WORK/sudoers" >>"$LOG" 2>&1; then
			echo -e "${COLOR_RED}Live sudo policy is invalid; refusing to build the ISO.${COLOR_RESET}"
			return 1
		fi

		# Autologin as the live user. Same file the installed system uses, rewritten rather than
		# removed — deleting it gives a login prompt for an account with no password set.
		mkdir -p "$WORK/gettyd"
		printf '[Unit]\nWants=NetworkManager.service\nAfter=NetworkManager.service\n[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin live --noclear %%I $TERM\n' \
			>"$WORK/gettyd/override.conf"
	fi

	# ---------------------------------------------------------------- who logs in, and their home
	#
	# WHOEVER AUTOLOGINS MUST HAVE A HOME IN THE IMAGE. Not "should" — the desktop does not start
	# without one, twice over: `~/.bash_profile` is what execs sway, and Electron needs a writable
	# HOME for its profile. Missing, the disc boots to a bash prompt, and typing `sway` by hand then
	# gives a black screen with no shell on it. Reported as both, three rebuilds apart, and they are
	# one fault.
	#
	# It was only ever arranged for the CLEAN path's `live` account. Answer `n` to the clean question
	# and the autologin stays as the operator — whose home is excluded anyway, because that is a
	# separate question with its own default. That combination produced an image where the person who
	# logs in has no home at all, and nothing in the build says so.
	#
	# So it is computed here from what the answers actually were, and the entries below are emitted
	# for that user whatever they were.
	local SESS_USER SESS_UID SESS_GID
	if [[ "$CLEAN" = *y* ]]; then
		SESS_USER="live"; SESS_UID=1000; SESS_GID=1000
	else
		# The account this machine autologins now — read from the unit, not guessed, because it is
		# the one the image will keep using.
		SESS_USER="$(sed -n 's/.*--autologin \([^ ]*\).*/\1/p' \
			/etc/systemd/system/getty@tty1.service.d/override.conf 2>/dev/null | head -1)"
		[[ -z "$SESS_USER" ]] && SESS_USER="$(awk -F: '$3>=1000 && $3<65534 {print $1; exit}' /etc/passwd)"
		SESS_UID="$(id -u "$SESS_USER" 2>/dev/null || echo 1000)"
		SESS_GID="$(id -g "$SESS_USER" 2>/dev/null || echo 1000)"
	fi
	echo -e "${COLOR_YELLOW}Live session logs in as: $SESS_USER${COLOR_RESET}"
	echo "session-user=$SESS_USER uid=$SESS_UID home-excluded=$([[ " ${EXCLUDES[*]} " == *" home/$SESS_USER "* ]] && echo yes || echo no)" >>"$LOG" 2>/dev/null

	# ---------------------------------------------------------------- a pseudo-file that REPLACES
	#
	# MKSQUASHFS SILENTLY IGNORES A PSEUDO-FILE WHOSE PATH ALREADY EXISTS IN THE SOURCE, and this
	# build depended on nine of them. Measured, against the real tool:
	#
	#     Pseudo file "etc/passwd" exists in source filesystem "src/etc/passwd".
	#     Ignoring, exclude it (-e/-ef) to override.
	#
	# One warning, on stdout, in the middle of packing a 45GB filesystem — and the image is written
	# successfully with the ORIGINAL file. So every replacement this build makes was being dropped
	# while the build reported success:
	#
	#   • /etc/passwd, /etc/group, /etc/shadow — the `live` account was never in the image. agetty
	#     then autologs in a user that does not exist, which is a login prompt: "posterchan live cd
	#     is totally shit ... booted to a terminal, no gui". Verified on the 4.1GB ISO the laptop
	#     built: /home/live existed (that path is NEW, because /home is excluded, so ITS pseudo
	#     applied) while /etc/passwd held no `live` at all.
	#   • the getty override — the image autologins `verita84`, the operator, on a disc scrubbed of
	#     that account. Both halves of one boot, disagreeing.
	#   • /etc/shadow — root stayed `!`, locked. "my root password 123456 don't even work".
	#   • /etc/fstab — THE LIVE IMAGE CARRIED THIS LAPTOP'S FSTAB, which mounts a LUKS root by UUID
	#     that is not in the machine being booted.
	#   • /etc/machine-id — every boot of every disc sharing one identity.
	#
	# The self-check could not see any of it: it asks whether /home/<user>/.bash_profile is there,
	# and that one was NEW, so it passed on every build.
	#
	# `pseudoput` emits the line and remembers the path, and every remembered path is excluded from
	# the source below. A directory is not recorded: a pseudo `d` on an existing directory is
	# harmless (it keeps the real one), and excluding e.g. etc/sudoers.d would throw away the
	# drop-ins the system needs to hold on to.
	# EVERY file pseudo goes through this, not only the ones known to clash. Excluding a path that
	# is not in the source costs nothing, while deciding case by case means asking "does this exist
	# on the machine being imaged?" -- and two of these DO on a box that has run the installer
	# before (/usr/local/share/posterchanos/gentoo.sh is the installer's own copy), which would
	# quietly ship the OLD installer on a disc built to carry the new one.
	PSEUDO_REPLACED=()
	pseudoput() { PSEUDO_REPLACED+=("$1"); echo "$@"; }

	local PSEUDO="$WORK/pseudo"
	# A ROOT-OWNED live boot gate. The package enablement link is retained, but a live image cannot
	# leave network startup to renderer timing or to a passwordless user command. Pull this unit into
	# the initial multi-user transaction and do not let tty1 (therefore Welcome) start before NM has
	# answered systemd. Keeping it live-image-only avoids changing the build host.
	cat >"$WORK/live-network.service" <<-'UNIT'
	[Unit]
	Description=Bring up networking before the PosterChanOS live desktop
	Requires=NetworkManager.service
	After=NetworkManager.service
	Before=getty@tty1.service

	[Service]
	Type=oneshot
	ExecStart=/usr/bin/systemctl is-active --quiet NetworkManager.service
	RemainAfterExit=yes
	UNIT
	cat >"$WORK/live-multi-user.conf" <<-'UNIT'
	[Unit]
	Requires=posterchan-live-network.service
	After=posterchan-live-network.service
	UNIT
	# Never inherit the desktop launcher from the machine doing the build. A package can leave the
	# 186 MB Electron binary installed while its /usr/local/bin wrapper is absent; that image passes
	# a binary-only check, starts Sway successfully, and then shows nothing but the black compositor
	# background because pc-shell-start has no command it can execute. Ship this small, deterministic
	# bridge as part of the live-image contract and read it back below.
	cat >"$WORK/posterchan-launcher" <<-'LAUNCHER'
	#!/bin/sh
	export APPDIR=/opt/posterchan
	export ELECTRON_OZONE_PLATFORM_HINT=auto
	if [ -x "$APPDIR/posterchan-desktop" ]; then exec "$APPDIR/posterchan-desktop" "$@"; fi
	exec "$APPDIR/AppRun" "$@"
	LAUNCHER
	chmod 0755 "$WORK/posterchan-launcher"
	# THE LIVE IMAGE GETS OUR SWAY CONFIG, NOT WHATEVER THIS BUILD HOST HAPPENS TO USE.
	#
	# Clean-image exclusions intentionally remove optional wallpapers. Copying the host's stock
	# Gentoo config therefore produced a disc that reached Sway and immediately raised:
	#
	#   Error on line 24 ... Unable to access ... Sway_Wallpaper_Blue_1920x1080.png
	#
	# More fundamentally, that config does not start pc-shell-start at all. Find the package-owned
	# config from either a source checkout or the synced overlay and REQUIRE its shell marker; a
	# missing session config is an image-build error, not something to discover after burning it.
	local LIVE_SWAY=""
	for F in \
		"$PCOS_TREE/overlay/app-misc/posterchanos-shell/files/sway.config" \
		"/var/db/repos/posterchan/app-misc/posterchanos-shell/files/sway.config"; do
		if [ -f "$F" ] && grep -q '/usr/local/bin/pc-shell-start' "$F"; then
			LIVE_SWAY="$F"; break
		fi
	done
	if [ -z "$LIVE_SWAY" ]; then
		echo -e "${COLOR_RED}PosterChanOS Sway config was not found; refusing to build a broken desktop.${COLOR_RESET}"
		echo "Looked beside gentoo.sh and in /var/db/repos/posterchan." >>"$LOG"
		return 1
	fi
	# Parse it before the multi-gigabyte squashfs is made. A headless backend lets validation run
	# from an SSH/build session with no seat; without it sway tries DRM first and reports a backend
	# failure before it ever reaches the config parser.
	mkdir -p "$WORK/sway-runtime" && chmod 0700 "$WORK/sway-runtime"
	if ! XDG_RUNTIME_DIR="$WORK/sway-runtime" WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 \
		sway -C -c "$LIVE_SWAY" >>"$LOG" 2>&1; then
		echo -e "${COLOR_RED}PosterChanOS Sway config is invalid; refusing to build the ISO.${COLOR_RESET}"
		return 1
	fi
	{
		pseudoput "etc/fstab" f 644 0 0 cat "$LIVEFSTAB"
		pseudoput "usr/local/bin/posterchan" f 755 0 0 cat "$WORK/posterchan-launcher"
		# Always replace /etc/sway/config. mksquashfs otherwise silently keeps the source host's file
		# when a pseudo-file targets an existing path (PSEUDO_REPLACED excludes it below).
		pseudoput "etc/sway/config" f 644 0 0 "cat \"$LIVE_SWAY\""
		# The installed machine's dracut.conf is host boot state, not live-image configuration.
		# bootloader() puts its encrypted-root UUID, unlock helper and LUKS key path here. /boot is
		# deliberately excluded from the squashfs, so retaining this file leaves the live session
		# asking dracut to install files that cannot exist. Worse, it publishes the source machine's
		# disk identity. The ISO initramfs is built explicitly below with an isolated configuration;
		# leave the live userland's copy empty so rebuilding an initramfs from the disc cannot inherit
		# the source laptop's encrypted-root recipe.
		pseudoput "etc/dracut.conf" f 644 0 0 echo -n
		# An EMPTY machine-id, not a copy of this machine's. systemd treats empty as "first boot"
		# and generates a fresh one; a duplicated id gives every live boot the same identity, which
		# breaks journald, DHCP leases and systemd-boot's own /boot layout.
		pseudoput "etc/machine-id" f 444 0 0 echo -n

		if [[ "$CLEAN" = *y* ]]; then
			pseudoput "etc/passwd" f 644 0 0 cat "$WORK/passwd"
			pseudoput "etc/group" f 644 0 0 cat "$WORK/group"
			pseudoput "etc/shadow" f 640 0 0 cat "$WORK/shadow"
			pseudoput "etc/sudoers" f 440 0 0 cat "$WORK/sudoers"
			pseudoput "etc/systemd/system/posterchan-live-network.service" f 644 0 0 cat "$WORK/live-network.service"
			echo "etc/systemd/system/multi-user.target.d d 755 0 0"
			pseudoput "etc/systemd/system/multi-user.target.d/posterchan-live-network.conf" f 644 0 0 cat "$WORK/live-multi-user.conf"
			echo "home d 755 0 0"
			# AN EMPTY HOME IS A TERMINAL, NOT A DESKTOP.
			#
			# What starts the GUI is `~/.bash_profile` — the login shell on tty1 execs sway, which
			# is how accounts() sets a real user up. Excluding /home and creating an empty
			# /home/live therefore produced a live image that autologged in correctly and dropped
			# straight to a bash prompt: "posterchan live cd is totally shit ... booted to a
			# terminal, no gui". The scrub removed the operator AND the one file that starts the
			# session, because on this system they live in the same directory.
			#
			# Written from the SAME heredoc the installer uses rather than a copy, so the live
			# session and an installed one cannot drift apart.

			echo "etc/systemd/system/getty@tty1.service.d d 755 0 0"
			pseudoput "etc/systemd/system/getty@tty1.service.d/override.conf" f 644 0 0 cat "$WORK/gettyd/override.conf"
			# A hostname that is not yours. `posterchanos` is what an unconfigured install should
			# call itself, and it is what the installer changes.
			pseudoput "etc/hostname" f 644 0 0 echo posterchanos
			# 0440, which is what sudo demands of a sudoers file — a mode it dislikes makes sudo
			# refuse to run AT ALL, not merely ignore the file, which would lock the disc out of
			# root far more thoroughly than having no drop-in.
			echo "etc/sudoers.d d 750 0 0"
			pseudoput "etc/sudoers.d/live" f 440 0 0 cat "$WORK/sudoers.d/live"
		fi

		# ---------------------------------------------------------------- the session's home
		#
		# EMITTED WHATEVER THE ANSWERS WERE. See SESS_USER above: the person who autologins needs a
		# home whether they are the clean image's `live` or this machine's own operator, and the two
		# questions that can remove it are asked separately.
		echo "home d 755 0 0"
		echo "home/$SESS_USER d 755 $SESS_UID $SESS_GID"
		echo "home/$SESS_USER/.bash_profile f 644 $SESS_UID $SESS_GID cat $WORK/live.bash_profile"
		echo "home/$SESS_USER/.config d 700 $SESS_UID $SESS_GID"
		echo "home/$SESS_USER/.config/sway d 700 $SESS_UID $SESS_GID"
		pseudoput "home/$SESS_USER/.config/sway/outputs.conf" f 600 "$SESS_UID" "$SESS_GID" echo -n

		# ---------------------------------------------------------------- the installer
		#
		# THE ISO CARRIES THE INSTALLER, or it is a demo disc.
		#
		# This builds a LIVE image of a running machine, and a live image with no way to install is
		# a thing you can look at and not a thing you can adopt. The installer is THIS script, so
		# the ISO gets a copy of the directory it was run from — gentoo.sh plus bin/ and plymouth/,
		# which it reads through $PCOS_TREE and half-works without.
		#
		# Injected as pseudo-files rather than copied into `/` first, for the reason the fstab
		# rewrite is: building an ISO must not modify the machine being imaged. And it must not come
		# from /home either — that is EXCLUDED by default, so an installer living in somebody's home
		# directory is precisely the file that would not be on the disc.
		local IHERE ISRC
		IHERE="$PCOS_TREE"
		if [[ -f "$IHERE/gentoo.sh" ]]; then
			echo "usr/local/share/posterchanos d 755 0 0"
			while IFS= read -r ISRC; do
				local REL="${ISRC#$IHERE/}"
				if [[ -d "$ISRC" ]]; then
					echo "usr/local/share/posterchanos/$REL d 755 0 0"
				else
					# 755 across the board: gentoo.sh must be executable, and the helpers in bin/ are
					# copied to /usr/local/bin by the install itself, which expects them to run.
					pseudoput "usr/local/share/posterchanos/$REL" f 755 0 0 "cat \"$ISRC\""
				fi
			done < <(find "$IHERE" -mindepth 1 \( -type f -o -type d \) | sort)

			# AND A WAY TO FIND IT. A terminal command nobody is told about is not a way to install
			# an operating system. The desktop's start menu lists every .desktop file on the machine
			# (see `_machineApps`), so an entry here puts "Install PosterChanOS" in the menu of the
			# live session with no extra wiring — the same list Firefox and the rest come from.
			echo "usr/share/applications d 755 0 0"
			pseudoput "usr/share/applications/posterchanos-install.desktop" f 644 0 0 cat "$WORK/install.desktop"
		fi
	} >"$PSEUDO"

	# The live user's login shell, identical to the one accounts() writes for a real user — see the
	# pseudo-file above.
	cat >"$WORK/live.bash_profile" <<'PROFILE'
[[ -f ~/.bashrc ]] && . ~/.bashrc
if [ -z "$WAYLAND_DISPLAY" ] && [ "$XDG_VTNR" = 1 ]; then
	# Welcome is the live machine's network setup UI, so its API must exist before Sway starts.
	# Enabled units remain the primary boot path; this also repairs stale enablement inherited from
	# a build host instead of presenting it to the user as missing network hardware.
	if [ "$(id -un)" = live ] && ! systemctl is-active --quiet NetworkManager.service; then
		sudo -n systemctl start NetworkManager.service
	fi
	export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=sway MOZ_ENABLE_WAYLAND=1
	exec sway
fi
PROFILE

	cat >"$WORK/install.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Install PosterChanOS
Comment=Install this system onto a disk
Icon=drive-harddisk
Exec=foot -T "Install PosterChanOS" -e sh -c 'installer=/usr/bin/gentoo.sh; [ -x "$installer" ] || installer=/usr/local/share/posterchanos/gentoo.sh; if [ "$(id -u)" = 0 ]; then exec "$installer" install-live; else exec sudo "$installer" install-live; fi'
Terminal=false
Categories=System;
DESKTOP

	# ---------------------------------------------------------------- squash it
	echo -e "${COLOR_YELLOW}Packing the filesystem — this is the slow part.${COLOR_RESET}"
	echo
	local EXARGS=()
	for f in "${EXCLUDES[@]}"; do EXARGS+=(-e "$f"); done
	# The source copy of anything a pseudo-file replaces, or the pseudo is ignored. See pseudoput.
	for f in "${PSEUDO_REPLACED[@]}"; do EXARGS+=(-e "$f"); done
	if ! mksquashfs / "$WORK/iso/LiveOS/squashfs.img" \
		-comp zstd -Xcompression-level 19 -b 1M -noappend -no-progress \
		-pf "$PSEUDO" "${EXARGS[@]}"; then
		echo
		_lcd_fail "mksquashfs failed — nothing was written."
		return
	fi

	# ---------------------------------------------------------------- did it actually work
	#
	# THE BUILD CHECKS ITS OWN OUTPUT, because three images in a row were written successfully and
	# were unusable, and the build said "done" every time. Both failures were one missing file:
	# without `~/.bash_profile` the autologin lands at a bash prompt ("booted to a terminal, no
	# gui"), and sway started by hand then has nowhere for the Electron shell to write its profile
	# ("sway loads a black screen"). An image that cannot start its desktop is not an image worth
	# spending twenty more minutes turning into an ISO.
	#
	# unsquashfs -l is a listing, not an extraction: it costs a second and touches nothing.
	if command -v unsquashfs >/dev/null 2>&1; then
		local MISSING=""
		local LS
		LS="$(unsquashfs -l "$WORK/iso/LiveOS/squashfs.img" 2>/dev/null)"
		echo "$LS" | grep -qx "squashfs-root/home/$SESS_USER" || MISSING="$MISSING /home/$SESS_USER"
		echo "$LS" | grep -qx "squashfs-root/home/$SESS_USER/.bash_profile" \
			|| MISSING="$MISSING /home/$SESS_USER/.bash_profile"
		# The desktop itself. An image with a home and no app boots to an empty sway.
		# The BINARY, not AppRun: AppRun exists only in an AppImage extraction, and the desktop now
		# installs from a plain tarball that has never had one. Either shape is accepted, so an
		# image built from an older release still passes.
		echo "$LS" | grep -qxE "squashfs-root/opt/posterchan/(posterchan-desktop|AppRun)" \
			|| MISSING="$MISSING /opt/posterchan"
		echo "$LS" | grep -qx "squashfs-root/usr/local/bin/posterchan" \
			|| MISSING="$MISSING /usr/local/bin/posterchan"
		# The welcome screen cannot configure wifi without the daemon, and launching getty before it
		# is ready creates the exact same visible failure as omitting it.
		echo "$LS" | grep -qx "squashfs-root/usr/lib/systemd/system/NetworkManager.service" \
			|| MISSING="$MISSING NetworkManager.service"
		echo "$LS" | grep -qx "squashfs-root/etc/systemd/system/multi-user.target.wants/NetworkManager.service" \
			|| MISSING="$MISSING NetworkManager-enable"
		echo "$LS" | grep -qx "squashfs-root/etc/systemd/system/posterchan-live-network.service" \
			|| MISSING="$MISSING live-network-gate"
		echo "$LS" | grep -qx "squashfs-root/etc/systemd/system/multi-user.target.d/posterchan-live-network.conf" \
			|| MISSING="$MISSING live-network-order"
		if [[ -n "$MISSING" ]]; then
			{ echo "image is missing:$MISSING"; echo "--- /home in the image ---";
			  echo "$LS" | grep '^squashfs-root/home' | head -20; } >>"$LOG" 2>/dev/null
			_lcd_fail "The image is missing:$MISSING — it would boot to a terminal, so it was not made into an ISO."
			return
		fi
		# Packages stay installed for recovery, but a release disc must not inherit services enabled
		# only on the builder. In particular, generating fresh host keys does not make an unattended
		# SSH daemon appropriate on every machine booted from the public ISO.
		if echo "$LS" | grep -qE '^squashfs-root/etc/systemd/system/(multi-user.target.wants/sshd.service|([^/]+\.)?target.wants/boot-snapshot.timer)$'; then
			_lcd_fail "The clean image inherited SSH or snapshot enablement from the build host — refusing to publish it."
			return
		fi
		# ---------------------------------------------------------- did the REPLACEMENTS land
		#
		# The listing above only proves a path exists, and every replaced file already existed --
		# with the WRONG contents. So these are read back OUT of the image. It is the check that
		# would have caught the ignored pseudo-files on the first build instead of the fourth ISO:
		# `unsquashfs -cat` costs a few kilobytes and answers what actually got written.
		if [[ "$CLEAN" = *y* ]]; then
			local WHO PW NET_ORDER
			if echo "$LS" | grep -qx 'squashfs-root/var/lib/posterchanos/admin-npub'; then
				_lcd_fail "The clean image still carries this machine's administrator claim, so Welcome would never appear — the ISO was not made."
				return
			fi
			WHO="$(unsquashfs -cat "$WORK/iso/LiveOS/squashfs.img" \
				etc/systemd/system/getty@tty1.service.d/override.conf 2>/dev/null \
				| sed -n "s/.*--autologin \([^ ]*\).*/\1/p" | head -1)"
			NET_ORDER="$(unsquashfs -cat "$WORK/iso/LiveOS/squashfs.img" \
				etc/systemd/system/getty@tty1.service.d/override.conf 2>/dev/null \
				| grep -c '^After=NetworkManager.service$')"
			PW="$(unsquashfs -cat "$WORK/iso/LiveOS/squashfs.img" etc/passwd 2>/dev/null \
				| grep -c "^$SESS_USER:")"
			echo "image: autologin=$WHO passwd-has-$SESS_USER=$PW" >>"$LOG" 2>/dev/null
			if [[ "$WHO" != "$SESS_USER" || "$PW" -lt 1 || "$NET_ORDER" -lt 1 ]]; then
				_lcd_fail "The image would log in as '${WHO:-nobody}' and its /etc/passwd has ${PW} such account. That is a login prompt, not a desktop — the ISO was not made. (mksquashfs ignores a pseudo-file whose path exists in the source; see pseudoput.)"
				return
			fi
		fi
		# A live image must not retain the source machine's encrypted-root recipe. Checking the
		# extracted contents (rather than merely the path) catches both an ignored pseudo-file and
		# any future regression that puts unlock.sh, a key path, or a disk UUID back into this file.
		local LIVE_DRACUT_CONF
		LIVE_DRACUT_CONF="$(unsquashfs -cat "$WORK/iso/LiveOS/squashfs.img" etc/dracut.conf 2>/dev/null)"
		if [[ -n "$LIVE_DRACUT_CONF" ]]; then
			{ echo "live /etc/dracut.conf was not empty:"; printf '%s\n' "$LIVE_DRACUT_CONF"; } >>"$LOG" 2>/dev/null
			_lcd_fail "The image retained this machine's dracut configuration — refusing to publish its encrypted-root UUID or key path."
			return
		fi
		local LIVE_DESKTOP_LAUNCHER
		LIVE_DESKTOP_LAUNCHER="$(unsquashfs -cat "$WORK/iso/LiveOS/squashfs.img" \
			usr/local/bin/posterchan 2>/dev/null)"
		if [[ "$LIVE_DESKTOP_LAUNCHER" != *'APPDIR=/opt/posterchan'* \
			|| "$LIVE_DESKTOP_LAUNCHER" != *'$APPDIR/posterchan-desktop'* ]]; then
			_lcd_fail "The image has no working PosterChan desktop launcher — it would boot to a black Sway screen."
			return
		fi
		echo -e "${COLOR_GREEN}Image checked: $SESS_USER has a home, a login profile, an account and a desktop.${COLOR_RESET}"
	fi

	# ---------------------------------------------------------------- kernel + a live initramfs
	#
	# WHERE THE KERNEL IS DEPENDS ON HOW IT WAS INSTALLED, and this script installs it the way that
	# does NOT put it where the old search looked. `systemd-boot` + `kernel-install` (both in this
	# installer's own USE flags) use the Boot Loader Spec layout:
	#
	#     /boot/<machine-id>/<kernel-version>/linux
	#     /boot/<machine-id>/<kernel-version>/initrd
	#
	# There is no `/boot/vmlinuz-*` anywhere on such a system, so `find /boot -name 'vmlinuz*'`
	# returned nothing and the build stopped with "No kernel found under /boot — cannot build a
	# bootable ISO", after packing the entire filesystem. Reported exactly that way.
	#
	# Both layouts are searched, newest last so `tail -1` prefers it, and the RUNNING version wins
	# when it is present — the squashfs was made from this filesystem, so its modules are the ones
	# under /lib/modules and a different kernel would boot without them.
	local KVER KERNEL
	KVER="$(uname -r)"
	KERNEL=""
	# 1. Boot Loader Spec, this exact kernel.
	KERNEL="$(ls -1 /boot/*/"$KVER"/linux 2>/dev/null | head -1)"
	# 2. The classic layout, this exact kernel.
	[[ -z "$KERNEL" ]] && KERNEL="$(ls -1 /boot/vmlinuz-"$KVER"* /boot/linux-"$KVER"* 2>/dev/null | head -1)"
	# 3. Where a Gentoo dist-kernel also keeps a copy.
	[[ -z "$KERNEL" && -f "/lib/modules/$KVER/vmlinuz" ]] && KERNEL="/lib/modules/$KVER/vmlinuz"
	[[ -z "$KERNEL" && -f "/usr/lib/modules/$KVER/vmlinuz" ]] && KERNEL="/usr/lib/modules/$KVER/vmlinuz"
	# 4. A MATCHED kernel/modules pair, newest. The old fallback selected any newest file under
	#    /boot but left KVER set to `uname -r`; dracut then built for one kernel while GRUB booted
	#    another. Generic graphics could still reach Welcome, but every loadable network driver was
	#    rejected as the wrong module version: "cannot see its network hardware".
	if [[ -z "$KERNEL" ]]; then
		local RUNNING_KVER="$KVER" CANDIDATE CANDIDATE_KERNEL
		while IFS= read -r CANDIDATE; do
			[[ -n "$CANDIDATE" ]] || continue
			CANDIDATE_KERNEL="$(ls -1 /boot/*/"$CANDIDATE"/linux 2>/dev/null | head -1)"
			[[ -z "$CANDIDATE_KERNEL" ]] && CANDIDATE_KERNEL="$(ls -1 /boot/vmlinuz-"$CANDIDATE"* /boot/linux-"$CANDIDATE"* 2>/dev/null | head -1)"
			[[ -z "$CANDIDATE_KERNEL" && -f "/lib/modules/$CANDIDATE/vmlinuz" ]] && CANDIDATE_KERNEL="/lib/modules/$CANDIDATE/vmlinuz"
			[[ -z "$CANDIDATE_KERNEL" && -f "/usr/lib/modules/$CANDIDATE/vmlinuz" ]] && CANDIDATE_KERNEL="/usr/lib/modules/$CANDIDATE/vmlinuz"
			if [[ -n "$CANDIDATE_KERNEL" ]]; then KVER="$CANDIDATE"; KERNEL="$CANDIDATE_KERNEL"; break; fi
		done < <(find /lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -Vr)
		[[ -n "$KERNEL" ]] && echo -e "${COLOR_YELLOW}The running kernel ($RUNNING_KVER) is not under /boot; using matched kernel/modules $KVER instead.${COLOR_RESET}"
	fi
	if [[ -z "$KERNEL" ]]; then
		{ echo "no kernel; /boot holds:"; ls -1 /boot 2>/dev/null | head -40; } >>"$LOG" 2>/dev/null
		ls -1 /boot 2>/dev/null | head -20
		_lcd_fail "No kernel found under /boot — looked for /boot/<machine-id>/$KVER/linux, /boot/vmlinuz-$KVER* and /lib/modules/$KVER/vmlinuz."
		return
	fi
	if [[ ! -d "/lib/modules/$KVER" ]]; then
		_lcd_fail "Kernel $KVER has no matching /lib/modules/$KVER tree. Booting it would hide network hardware."
		return
	fi
	# The squashfs was already packed, so prove the selected modules actually landed in the image.
	# A source directory existing is insufficient when an exclude or a subordinate mount removed it.
	if command -v unsquashfs >/dev/null 2>&1 \
		&& ! printf '%s\n' "$LS" | grep -qE "^squashfs-root/(usr/)?lib/modules/$KVER(/|$)"; then
		_lcd_fail "The image does not contain /lib/modules/$KVER for its kernel. Network and GPU drivers would not load."
		return
	fi
	if command -v unsquashfs >/dev/null 2>&1 \
		&& ! printf '%s\n' "$LS" | grep -qE '^squashfs-root/(usr/)?lib/firmware(/|$)'; then
		_lcd_fail "The image contains no Linux firmware tree. Common Wi-Fi adapters would be invisible."
		return
	fi
	echo -e "${COLOR_YELLOW}Kernel: $KERNEL${COLOR_RESET}"
	echo "kernel=$KERNEL" >>"$LOG" 2>/dev/null
	cp -f "$KERNEL" "$WORK/iso/boot/vmlinuz"

	echo
	echo -e "${COLOR_YELLOW}Building a live initramfs (not the host-only one).${COLOR_RESET}"
	echo
	# --no-hostonly is the load-bearing flag. The installed initramfs is built for THIS machine and
	# carries only its drivers, plus the LUKS unlock for its disk; booted on somebody else's laptop
	# it finds no root and drops to an emergency shell. This one carries every driver dracut has.
	#
	# AND IT MUST NOT READ THIS MACHINE'S DRACUT CONFIG, which is two problems wearing one hat.
	#
	# "live cd error: dracut failed the iso would not boot / module systemd-cryptsetup depends on
	# module crypt". An encrypted install writes `add_dracutmodules+=" crypt systemd-cryptsetup dm
	# rootfs-block "` into /etc/dracut.conf (see hibernation/bootloader above), and dracut reads that
	# file whatever the command line says. So the config ADDED systemd-cryptsetup while this line
	# OMITTED crypt, and dracut refused the contradiction — correctly. Omitting crypt is right: a
	# live image boots from a squashfs on an ISO, not from a LUKS disk.
	#
	# The second problem is the one nobody would have noticed. That same file carries
	# `install_items+=" /boot/unlock.sh /boot/keyfile.key "` — the key that unlocks THIS machine's
	# disk without a password. Inherited here, every ISO built on an encrypted install would have
	# shipped that key inside its initramfs, on a disc meant to be handed to somebody, defeating the
	# entire "clean out this machine's accounts and secrets" pass a few dozen lines above.
	#
	# `--conf /dev/null --confdir` with an empty directory is dracut's own way to say "this build
	# starts from nothing". systemd-cryptsetup is nevertheless auto-selected on this system, and it
	# has a hard dependency on the crypt module deliberately omitted for a public live image. Omit
	# both sides: the empty configuration is what keeps the host keyfile out.
	mkdir -p "$WORK/dracut.conf.d"
	dracut --force --no-hostonly --nolvmconf --nomdadmconf \
		--conf /dev/null --confdir "$WORK/dracut.conf.d" \
		--add "dmsquash-live" --omit "crypt crypt-gpg crypt-loop systemd-cryptsetup" \
		--kver "$KVER" "$WORK/iso/boot/initramfs.img" 2>&1 | tee -a "$LOG"
	# PIPESTATUS, not the pipeline's — `tee` succeeds whatever dracut did.
	if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
		echo
		_lcd_fail "dracut failed (kernel $KVER) — the ISO would not boot, so nothing was written."
		return
	fi

	# ---------------------------------------------------------------- boot menu
	#
	# `root=live:CDLABEL=…` is how dmsquash-live finds the medium, so the label here and the one
	# passed to grub-mkrescue below MUST match — a mismatch boots to a dracut shell with no clue
	# as to why.
	cat >"$WORK/iso/boot/grub/grub.cfg" <<GRUB
set default=0
set timeout=5
insmod all_video
menuentry "PosterChan Live" {
    linux /boot/vmlinuz root=live:CDLABEL=$LABEL rd.live.image rd.live.dir=LiveOS rd.live.squashimg=squashfs.img quiet
    initrd /boot/initramfs.img
}
menuentry "PosterChan Live (verbose)" {
    linux /boot/vmlinuz root=live:CDLABEL=$LABEL rd.live.image rd.live.dir=LiveOS rd.live.squashimg=squashfs.img rd.debug
    initrd /boot/initramfs.img
}
menuentry "PosterChan Live (copy to RAM)" {
    linux /boot/vmlinuz root=live:CDLABEL=$LABEL rd.live.image rd.live.dir=LiveOS rd.live.squashimg=squashfs.img rd.live.ram=1 quiet
    initrd /boot/initramfs.img
}
GRUB

	# ---------------------------------------------------------------- the ISO
	echo
	echo -e "${COLOR_YELLOW}Writing $ISO${COLOR_RESET}"
	echo
	# ISO levels 1 and 2 cap every individual file at 4 GiB.  The squashfs is deliberately one
	# contiguous image and a normal desktop root can exceed that even after compression, so request
	# level 3 explicitly.  Without it grub-mkrescue finishes all expensive work and xorriso then
	# rejects LiveOS/squashfs.img at the final step.
	grub-mkrescue -o "$ISO" "$WORK/iso" -volid "$LABEL" -iso-level 3 2>&1 | tee -a "$LOG"
	# PIPESTATUS, not the pipeline's — see the note above. `tee` succeeds whatever grub-mkrescue did.
	if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
		echo
		_lcd_fail "grub-mkrescue failed — no ISO was written."
		return
	fi

	rm -rf "$WORK"
	echo
	echo -e "${COLOR_GREEN}◆ DONE ◆${COLOR_RESET}"
	echo -e "    $ISO  ($(du -h "$ISO" | cut -f1))"
	echo
	echo -e "${COLOR_CYAN}Write it to a USB stick with:${COLOR_RESET}"
	echo -e "    dd if=$ISO of=/dev/sdX bs=4M status=progress oflag=sync"
	echo
	echo -e "${COLOR_YELLOW}It is a hybrid image: the same file boots on BIOS and on UEFI.${COLOR_RESET}"
	echo
	# A headless `gentoo.sh livecd` has no keyboard. The image is already complete here, so an
	# unconditional read leaves the ssh job and its caller hanging forever after a successful build.
	[[ -t 0 ]] && read -p "Press enter key to Continue"
	# The test above is false for deployment automation. Do not let that harmless false condition
	# turn a completed image into a failed build status.
	return 0
}

download-setup() {
	clear
	echo -e "\033[1;36m[Choose Deployment Type]\033[0m"
	echo
	echo
	setDevices

	if [[ $REPO_CHOICE = *local* ]]; then
		STAGE3_URL="https://gentoo.poster.place/releases/amd64/autobuilds/current-stage3-amd64-systemd/$(
			curl -q https://gentoo.poster.place/releases/amd64/autobuilds/current-stage3-amd64-systemd/ | grep -i stage3-amd64-systemd | grep -Ev 'CONTENTS|DIGESTS|sha|.asc' | grep ".tar.xz" | cut -d '>' -f2 | cut -d '<' -f1
		)"
	else
		STAGE3_URL=$(curl https://www.gentoo.org/downloads/ | grep -i stage3-amd64-systemd | head -1 | cut -d '"' -f2-3 | cut -d '"' -f1)
	fi

	STAGE3_FILE="/tmp/stage3.tar.xz"
	if [ -f "$STAGE3_FILE" ]; then
		echo
		echo -e "\033[1;33mStage 3 already downloaded.....\033[0m"
		echo
	else
		wget -O /tmp/stage3.tar.xz "$STAGE3_URL"
	fi

	if [ -f "$STAGE3_FILE" ]; then
		echo
		echo
		echo -e "\033[1;33mExtracting Tar File..........\033[0m"
		echo
		echo
		systemMounts
		echo
		echo -e "\033[1;33mExtracting $STAGE3_FILE\033[0m"
		echo
		tar xf $STAGE3_FILE -C $TARGET/
		fstab
		cp -f /etc/resolv.conf $TARGET/etc/
		configurePortage
		cp -f gentoo.sh $TARGET/usr/bin/
	fi
}

menu() {
	clear
	echo
	echo -e "\033[1;36m═══════════════════════════════════════════════════════\033[0m"
	echo -e "\033[1;97m  ⚡ POSTERCHANOS INSTALLER ⚡\033[0m"
	echo -e "\033[1;36m═══════════════════════════════════════════════════════\033[0m"
	echo
	echo -e "\033[1;36m[1] ▶ Setup Disk\033[0m"
	echo -e "\033[1;35m[2] ▶ Download Gentoo Installation Files\033[0m"
	echo -e "\033[1;33m[3] ▶ Install Gentoo, LOL\033[0m"
	echo -e "\033[1;32m[4] ▶ Reinstall Bootloader\033[0m"
	echo -e "\033[1;36m[5] ▶ Initialize Disk\033[0m"
	echo
	echo -e "\033[1;35m═══════════════════════════════════════════════════════\033[0m"
	echo -e "\033[1;35m            ◆ POSTINSTALL / TROUBLESHOOTING ◆\033[0m"
	echo -e "\033[1;35m═══════════════════════════════════════════════════════\033[0m"
	echo
	echo -e "\033[1;33m[6] ▶ Backup/Restore Live OS\033[0m"
	echo -e "\033[1;36m[9] ▶ Install this Live image to a disk\033[0m"
	echo -e "\033[1;32m[7] ▶ Backup OS to Build Server\033[0m"
	echo -e "\033[1;36m[8] ▶ Tools and Tweaks\033[0m"
	echo
	read -p 'Your Choice: ' choice

	if [[ $choice = 1 ]]; then
		setDevices
		read -p "Press enter key to Continue"
		menu
	elif [[ $choice = 2 ]]; then
		echo
		echo -e "\033[1;36m[Repository Choice]\033[0m"
		echo
		echo -e "\033[1;33mDo you want to use your local repo or the official Gentoo Repo?\033[0m"
		echo
		read -p 'local or remote:' -e -i "local" REPO_CHOICE
		download-setup
		read -p "Press enter key to Continue"
		menu
	elif [[ $choice = 3 ]]; then
		setDevices
		buildGentoo
		read -p "Press enter key to Continue"
		menu
	elif [[ $choice = 4 ]]; then
		bootloader
	elif [[ $choice = 5 ]]; then
		clear
		echo -e "\033[1;36m[Initialize Disk]\033[0m"
		echo
		echo
		setDevices
		partitionDetection
		initializeDisk
		read -p "Press enter key to Continue"
		menu
	elif [[ $choice = 6 ]]; then
		clear
		setDevices
		read -p 'Are you restoring from a build server? ' -e -i "n" QUESTION_BUILD_SERVER
		if [[ $QUESTION_BUILD_SERVER = *y* ]]; then
			BUILD_SERVER="y"
		fi
	
		liveOSrestore "$HARD_DISK" $ROOT_MAPPER_NAME "none" "none" "$ROOT_NAME"

	elif [[ $choice = 9 ]]; then
		clear
		liveISOinstall
		read -p "Press enter key to Continue"
		menu
	elif [[ $choice = 7 ]]; then
		clear
		backupOS
	elif [[ $choice = 8 ]]; then
		tweaks
	else
		menu
	fi
}

partitions() {
	echo
	echo -e "\033[1;35m◆ SETTING UP PARTITIONS ◆\033[0m"
	printf "$DISK_PASSWORD" | cryptsetup open ${BTRFS} $(echo $ROOT_MAPPER_NAME | sed 's/\/dev\/mapper\///')

	if [[ -e "$ROOT_MAPPER_NAME" ]]; then
		fstab
	else
		echo
		echo -e "\033[1;33mPartitions: Aborting Install, $ROOT_MAPPER_NAME not found!\033[0m"
		echo
		echo
		exit 1
	fi

}

setDevices() {
	if [ -f "/tmp/disk" ]; then
		HARD_DISK=$(cat /tmp/disk | head -1)
		ROOT_NAME=$(cat /tmp/disk | tail -2 | head -1)
		SWAP_CHOICE=$(cat /tmp/disk | tail -1 | head -1)
		partitionDetection
		echo
		echo -e "\033[1;33mConfiguration Settings:\033[0m"
		echo
		echo -e "\033[1;33mDisk: $HARD_DISK\033[0m"
		echo -e "\033[1;33mRoot Name: $ROOT_NAME\033[0m"
		echo -e "\033[1;33mRoot Mapper Name: $ROOT_MAPPER_NAME\033[0m"
		echo -e "\033[1;33mSwap Choice: $SWAP_CHOICE\033[0m"
		echo
		echo
	else
		clear
		echo
		echo -e "\033[1;33mDisks and Partitions:\033[0m"
		echo
		lsblk -e7 -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL
		echo
		local LIVE_SOURCE LIVE_PART LIVE_DISK DEFAULT_DISK
		LIVE_SOURCE="$(findmnt -no SOURCE /run/initramfs/live 2>/dev/null)"
		LIVE_PART="${LIVE_SOURCE#/dev/}"
		LIVE_DISK="$(lsblk -ndo PKNAME "$LIVE_SOURCE" 2>/dev/null)"
		[ -n "$LIVE_DISK" ] || LIVE_DISK="$LIVE_PART"
		# QEMU commonly exposes a legacy /dev/fd0 before its virtio disk.  It reports TYPE=disk but is
		# neither a writable installation target nor even probeable, so the old "first disk" rule chose
		# it and failed at wipefs before touching the real vda. Optical, loop/RAM and implausibly small
		# devices are equally unsuitable defaults. The user may still explicitly name another genuine
		# whole disk below; this filter only governs the safe one-click default.
		DEFAULT_DISK="$(lsblk -bdnro NAME,TYPE,SIZE | awk -v live="$LIVE_DISK" \
			'$2=="disk" && $1!=live && $1!~/^(fd|sr|zram|loop|ram)/ && $3>=8589934592 {print $1; exit}')"
		[ -n "$DEFAULT_DISK" ] || { echo "No install disk found besides the live medium."; return 1; }
		read -r -p "Disk Device to Use [$DEFAULT_DISK]: " device
		device="${device:-$DEFAULT_DISK}"
		if [ ! -b "/dev/$device" ] || [ "$(lsblk -dnro TYPE "/dev/$device" 2>/dev/null)" != disk ]; then
			echo "Not a whole disk: /dev/$device"; return 1
		fi
		if [ "$device" = "$LIVE_DISK" ]; then
			echo "Refusing to install onto the live boot disk /dev/$device"; return 1
		fi

		read -r -p 'BTRFS Root Volume name [gentoo]: ' root_name
		root_name="${root_name:-gentoo}"

		HARD_DISK=$device
		echo $HARD_DISK >/tmp/disk
		echo $root_name >>/tmp/disk
		echo none >>/tmp/disk
		setDevices
	fi
	partitionDetection
}

hibernation() {
	echo "[Sleep]" >/etc/systemd/sleep.conf
	echo "AllowSuspend=yes" >>/etc/systemd/sleep.conf
	echo "AllowHibernation=yes" >>/etc/systemd/sleep.conf
	echo "AllowSuspendThenHibernate=yes" >>/etc/systemd/sleep.conf
	echo "HibernateState=disk" >>/etc/systemd/sleep.conf
	echo "HibernateMode=platform" >>/etc/systemd/sleep.conf
	echo "HibernateDelaySec=500" >>/etc/systemd/sleep.conf
	echo "HandleLidSwitch=suspend-then-hibernate" >>/etc/systemd/logind.conf
	echo "HandleLidSwitchExternalPower=suspend-then-hibernate" >>/etc/systemd/logind.conf
	unlink /usr/lib/systemd/system/systemd-suspend.service
}

# Enable hibernation on an already-installed PosterChanOS machine. This is also the backend for
# System Settings, so it is deliberately non-interactive and idempotent.
hibernateSetup() {
	if [ "$(id -u)" != "0" ]; then
		echo "hibernation setup needs administrator access" >&2; return 1
	fi
	local swap=/swap/swap ram uuid offset conf entry
	mkdir -p /swap /etc/dracut.conf.d
	if [ ! -f "$swap" ]; then
		ram="$(awk '/MemTotal:/ {printf "%dM", ($2/1024)+1024}' /proc/meminfo)"
		if ! command -v btrfs >/dev/null || ! btrfs filesystem mkswapfile --size "$ram" "$swap"; then
			echo "could not create the Btrfs hibernation swapfile" >&2; return 1
		fi
	fi
	chmod 0600 "$swap"
	grep -qF "$swap none swap" /etc/fstab 2>/dev/null || echo "$swap none swap defaults 0 0" >>/etc/fstab
	swapon "$swap" 2>/dev/null || true
	offset="$(btrfs inspect-internal map-swapfile -r "$swap" 2>/dev/null)"
	uuid="$(findmnt -no UUID -T "$swap" 2>/dev/null | head -1)"
	if [ -z "$uuid" ] || [ -z "$offset" ]; then
		echo "could not determine the swapfile resume address" >&2; return 1
	fi
	conf=/etc/dracut.conf.d/90-posterchan-hibernate.conf
	cat >"$conf" <<-EOF
	add_dracutmodules+=" resume "
	kernel_cmdline+=" resume=UUID=$uuid resume_offset=$offset "
	EOF
	# Keep Boot Loader Specification entries bootable even when the installed dracut build does not
	# import its embedded command line early enough for systemd's resume generator.
	for entry in /boot/loader/entries/*.conf; do
		[ -f "$entry" ] || continue
		sed -i -E 's/[[:space:]]+resume=(UUID=)?[^[:space:]]+//g; s/[[:space:]]+resume_offset=[^[:space:]]+//g' "$entry"
		sed -i -E "s#^options (.*)#options \\1 resume=UUID=$uuid resume_offset=$offset#" "$entry"
	done
	hibernation
	if command -v dracut >/dev/null; then
		dracut --regenerate-all --force || { echo "dracut could not rebuild the initramfs" >&2; return 1; }
	else
		echo "dracut is not installed" >&2; return 1
	fi
	echo "hibernation enabled; reboot once before using it"
}

bootloader() {
	# Fresh installs hand the actual user-selected LUKS password through the chroot environment.
	# Repair invocations without it retain the legacy interactive/script default for compatibility.
	[ -n "${PC_INSTALL_PASSWORD:-}" ] && DISK_PASSWORD="$PC_INSTALL_PASSWORD"
	# dracut requires a real temporary directory. Live images exclude /var/tmp contents, and an
	# absent directory made an otherwise-correct encrypted-root rebuild fail.
	mkdir -p /var/tmp
	chmod 1777 /var/tmp
	[ ! -e /boot/EFI ] || chmod -R 740 /boot/EFI
	rm -rf /boot/loader/entries/*
	if [ -f "/etc/disk" ]; then
		partitionDetection
		echo
		echo -e "\033[1;33mInstalling Bootloader...................\033[0m"
		sleep 3
		echo
		# THE ENTRY IS WRITTEN WITH `>`, WHICH CANNOT CREATE A DIRECTORY -- and every line of it
		# failed, silently, on a machine that then booted to a menu holding nothing but "Reboot Into
		# Firmware Interface". Reproduced against a fake target: six "No such file or directory"
		# lines on stderr, an install that reports success, and a disk with a kernel, an initramfs
		# and no way to name them.
		#
		# `bootctl install` normally creates /boot/loader/entries. In a chroot its automatic ESP and
		# EFI-variable discovery describes the live host, not the target, so use the explicit offline
		# form below and require its fallback executable before writing any text entries.
		# Install for a target mounted in a chroot: do not inspect the live host's udev state or
		# modify its EFI variables. The fallback loader is what fresh VM NVRAM boots first.
		if ! bootctl --esp-path=/boot --no-variables install; then
			echo -e "\033[1;31m  bootctl could not install systemd-boot on the target ESP.\033[0m"
			return 1
		fi
		if [ ! -s /boot/EFI/BOOT/BOOTX64.EFI ]; then
			echo -e "\033[1;31m  EFI/BOOT/BOOTX64.EFI is missing from the target ESP.\033[0m"
			return 1
		fi
		mkdir -p /boot/loader/entries
		MACHINE_ID=$(cat /etc/machine-id 2>/dev/null)
		if [ -z "$MACHINE_ID" ]; then
			systemd-machine-id-setup >/dev/null 2>&1 || true
			MACHINE_ID=$(cat /etc/machine-id 2>/dev/null)
		fi
		if [ -z "$MACHINE_ID" ]; then
			echo -e "\033[1;31mNo machine ID — refusing to delete modules or write invalid /boot paths.\033[0m"
			return 1
		fi
		# The directory name already IS the complete kernel version. Splitting `kernel-$version`
		# into a fixed number of dash-separated fields truncated versions such as
		# 6.18.43-gentoo-dist-bin to 6.18.43-gentoo-dist, so the entry named files that did not exist.
		KERNEL_VERSION="$(find "/boot/$MACHINE_ID" -mindepth 1 -maxdepth 1 -type d \
			-printf '%f\n' 2>/dev/null | sort -V | tail -1)"
		KERNEL="kernel-$KERNEL_VERSION"
		# THE VERSION IS READ FROM A DIRECTORY, SO A MISSING DIRECTORY IS AN EMPTY VERSION -- and
		# every line below then builds a path with a hole in it. The loader entry names
		# `/<machine-id>//linux`, `mkdir -p /boot/$MACHINE_ID/$KERNEL_VERSION` makes a directory
		# with no version in it, and the module cleanup runs `grep -Evi` with no pattern at all.
		# The entry gets written, the install says it finished, and the machine boots into
		# emergency mode -- which is where this was found, and it says nothing about a kernel.
		#
		# Rebuild the layout from what IS on the disk before giving up on it: /boot/vmlinuz is the
		# kernel the live installer copies off the medium, and /usr/lib/modules names its version.
		if [ -z "$KERNEL_VERSION" ]; then
			KERNEL_VERSION="$(ls /usr/lib/modules 2>/dev/null | sort -V | tail -1)"
			if [ -n "$KERNEL_VERSION" ] && [ -f /boot/vmlinuz ]; then
				echo -e "\033[1;33mNo kernel under /boot/$MACHINE_ID — placing $KERNEL_VERSION there\033[0m"
				mkdir -p "/boot/$MACHINE_ID/$KERNEL_VERSION"
				cp -f /boot/vmlinuz "/boot/$MACHINE_ID/$KERNEL_VERSION/linux"
				KERNEL="kernel-$KERNEL_VERSION"
			else
				echo -e "\033[1;31mNo kernel to boot: /boot/$MACHINE_ID is empty and there is no\033[0m"
				echo -e "\033[1;31m/boot/vmlinuz. Not writing a boot entry that names one.\033[0m"
				return 1
			fi
		fi
		LOADER_FILE="/boot/loader/entries/$MACHINE_ID-$KERNEL_VERSION.conf"
		PREVIOUS_LOADER_FILE="/boot/loader/entries/previous.conf"
		OFFSET=$(btrfs inspect-internal map-swapfile /swap/swap -r)
		UUID=$(/usr/bin/findmnt -no UUID -T /swap/swap | head -1)

		KERNEL_COMMAND_LINE="quiet splash usbcore.quirks=0bda:8156,0bda:8153 rd.luks.key=/boot/keyfile.key mitigations=off resume=UUID=$UUID resume_offset=$OFFSET root=UUID=$(/sbin/blkid -s UUID -o value $ROOT_MAPPER_NAME) rootflags=subvol=@$ROOT_NAME rw"
		rm -f /etc/crypttab
		echo >/etc/dracut.conf
		mkdir -p /boot/$MACHINE_ID/$KERNEL_VERSION

	# This is another kernel argument, not punctuation on the preceding `rw`.  The old colon
	# produced a literal `rw:` argument and left the encrypted volume discovery to accident.
	KERNEL_COMMAND_LINE="$KERNEL_COMMAND_LINE rd.luks.uuid=luks-$(/sbin/blkid -s UUID -o value ${BTRFS})"
        dracut_modules=" crypt systemd-cryptsetup dm rootfs-block "
        echo "add_dracutmodules+=\" $dracut_modules \"" >> /etc/dracut.conf  
		echo "kernel_cmdline+=\" $KERNEL_COMMAND_LINE \" " >>/etc/dracut.conf

		echo "$(echo $ROOT_MAPPER_NAME | sed 's/\/dev\/mapper\///') UUID=$(/sbin/blkid -s UUID -o value ${BTRFS})  none luks" >/etc/crypttab

		if [ "$AUTO_DECRYPT" == "True" ]; then
			if ! decryptBoot "$BTRFS"; then
				echo -e "\033[1;31mCould not install the encrypted-root keyfile.\033[0m"
				return 1
			fi
		fi

        echo -e "\033[1;33mDeleting old Kernel Modules\033[0m"
        echo
        cd /usr/lib/modules
        ls /usr/lib/modules | grep -Evi "$KERNEL_VERSION" | xargs -r rm -r
		# REBUILD THE INITRD THE LOADER ACTUALLY USES. `--regenerate-all` writes the conventional
		# /boot/initramfs-$KERNEL_VERSION.img, but the entry below boots the Boot Loader Spec path
		# /boot/$MACHINE_ID/$KERNEL_VERSION/initrd.  liveISOinstall() had already put a preliminary
		# initrd there before crypttab, the keyfile and the final kernel command line existed, so the
		# regenerated encrypted-root image sat unused while systemd-boot loaded the old one and
		# dropped into maintenance mode.
		INITRD="/boot/$MACHINE_ID/$KERNEL_VERSION/initrd"
		# PLYMOUTH IS EMBEDDED IN THE INITRAMFS. Selecting the PosterChan theme after dracut means
		# this boot still contains Gentoo's default and the new choice appears only after some later
		# kernel rebuild. Choose it first, then build the image that systemd-boot actually names.
		if ! _pc_select_plymouth_theme; then
			echo -e "\033[1;31mCould not select the PosterChanOS boot splash.\033[0m"
			return 1
		fi
		if ! dracut --force --add "$dracut_modules" "$INITRD" "$KERNEL_VERSION"; then
			echo -e "\033[1;31mCould not build the encrypted-root initramfs at $INITRD.\033[0m"
			return 1
		fi
		# Prove the automatic-unlock artifact, not some other initramfs, carries its LUKS recipe and
		# key. Passphrase installs deliberately have no embedded keyfile; dracut's successful exit is
		# their proof and also keeps this function testable with a stub initramfs.
		if [ "$AUTO_DECRYPT" == "True" ]; then
			if ! lsinitrd "$INITRD" 2>/dev/null | grep -q 'etc/crypttab' \
				|| ! lsinitrd "$INITRD" 2>/dev/null | grep -q 'boot/keyfile.key' \
				|| ! lsinitrd "$INITRD" 2>/dev/null | grep -q 'systemd-cryptsetup'; then
				echo -e "\033[1;31m$INITRD is missing crypttab, the keyfile, or systemd-cryptsetup.\033[0m"
				echo -e "\033[1;31mInstallation stopped instead of writing an unbootable entry.\033[0m"
				return 1
			fi
		fi
		mkdir -p /boot/$MACHINE_ID/$KERNEL_VERSION
		echo -e "\033[1;33mMachineID=$MACHINE_ID\033[0m"
		echo -e "\033[1;33mKERNEL: $KERNEL\033[0m"
		echo -e "\033[1;33mKERNEL_VERSION: $KERNEL_VERSION\033[0m"
		echo -e "\033[1;33mRoot_Name: $ROOT_NAME\033[0m"
		echo -e "\033[1;33mBTRFS: $BTRFS\033[0m"
		echo -e "\033[1;33mUEFI Kernel: $KERNEL_VERSION\033[0m"
		echo -e "\033[1;33mSWAP UUID=$UUID\033[0m"
		echo -e "\033[1;33mOFFSET=$OFFSET\033[0m"
		echo "default $MACHINE_ID-*" >/boot/loader/loader.conf
		echo "timeout 1" >>/boot/loader/loader.conf

		echo
		echo
		echo

		#Generate Main Boot Entry
		echo "title Current" >$LOADER_FILE
		echo "version $KERNEL_VERSION" >>$LOADER_FILE
		echo "options $KERNEL_COMMAND_LINE " >>$LOADER_FILE
		echo "machine-id $MACHINE_ID" >>$LOADER_FILE
		echo "linux /$MACHINE_ID/$KERNEL_VERSION/linux" >>$LOADER_FILE
		echo "initrd /$MACHINE_ID/$KERNEL_VERSION/initrd" >>$LOADER_FILE

		# READ BACK WHAT WAS WRITTEN. Everything above is `echo >` into paths built from variables,
		# and the failure this exists for produced no output a person would see and no exit code
		# anyone checked -- the install said "Complete!" and the machine had no entries at all.
		#
		# systemd-boot DROPS an entry whose `linux` file is missing, so an entry that exists is not
		# the same as an entry that boots: both are checked, and named when they are wrong.
		if [ ! -s "$LOADER_FILE" ]; then
			echo -e "\033[1;31m◆ NO BOOT ENTRY WAS WRITTEN ◆\033[0m"
			echo -e "\033[1;31m  $LOADER_FILE is missing or empty. This disk will boot to a menu\033[0m"
			echo -e "\033[1;31m  with nothing in it. Check that /boot is the mounted EFI partition.\033[0m"
		elif [ ! -f "/boot/$MACHINE_ID/$KERNEL_VERSION/linux" ]; then
			echo -e "\033[1;31m◆ THE BOOT ENTRY NAMES A KERNEL THAT IS NOT THERE ◆\033[0m"
			echo -e "\033[1;31m  /boot/$MACHINE_ID/$KERNEL_VERSION/linux does not exist, and\033[0m"
			echo -e "\033[1;31m  systemd-boot hides an entry it cannot find a kernel for.\033[0m"
		elif [ ! -f "/boot/$MACHINE_ID/$KERNEL_VERSION/initrd" ]; then
			echo -e "\033[1;33m  the entry has no initramfs beside it — dracut did not run here.\033[0m"
		else
			echo -e "\033[1;32m◆ BOOT ENTRY WRITTEN ◆ $LOADER_FILE\033[0m"
			echo -e "\033[1;32m  kernel + initramfs are in /boot/$MACHINE_ID/$KERNEL_VERSION\033[0m"
		fi
	else
		echo -e "\033[1;33mError, Missing /etc/disk\033[0m"
		exit 1
	fi
}

compile-kernel() {
	cd /usr/src/linux
	time make -j50 CC="distcc gcc"
	make -j50 CC="distcc gcc" modules_install
	make install
}

fixBase() {
	sudo emerge libudev libcap glibc go sys-apps/acl sys-apps/util-linux

}

fix-build-boot() {
	ssh $USER@$BUILD_SERVER_ADDRESS "sudo gentoo.sh fstab"
	ssh $USER@$BUILD_SERVER_ADDRESS "sudo gentoo.sh bootloader"
	ssh $USER@$BUILD_SERVER_ADDRESS "sudo reboot"
}

if [ "$1" = "services" ]; then
	services
elif [ "$1" = "upgrade-system" ]; then
	upgrade-system
elif [ "$1" = "fstab" ]; then
	partitionDetection
	export TARGET=/
	fstab
elif [ "$1" = "upgrade" ]; then
    updateOS
elif [ "$1" = "wifi" ]; then
	wifi
elif [ "$1" = "accounts" ]; then
	accounts
elif [ "$1" = "hibernate" ]; then
	hibernateSetup
elif [ "$1" = "bootloader" ]; then
	# This entry is invoked from inside the target chroot by finalizeInstall. The script's historical
	# top-level default is /tmp/install for host-side operations; retaining it here makes Plymouth
	# inspect /tmp/install/usr/share inside the new root instead of the actual installed system.
	export TARGET=/
	bootloader
elif [ "$1" = "steam" ]; then
	installSteam
elif [ "$1" = "install-packages" ]; then
	installPackages
elif [ "$1" = "posterchan-shell" ]; then
	# Called from INSIDE the chroot during finalize, where the new root is `/` and TARGET must not
	# point anywhere else. The script assigns TARGET='/tmp/install' at load, so it is cleared here.
	export TARGET=/
	posterchanShell
elif [ "$1" = "shell" ]; then
	# The same thing on a machine that is already running — after an etc-update has replaced the
	# sway config with the package default, which is what `emerge` does to a file portage owns.
	export TARGET=/
	posterchanShell
	plymouthTheme
elif [ "$1" = "splash" ]; then
	export TARGET=/
	plymouthTheme
# UNATTENDED ENTRY POINTS. Driving the MENU from a pipe is how this was run the first time, and when
# the piped input ran out `read` returned instantly, `menu` recursed on every empty answer, and bash
# died of a stack overflow — "Segmentation fault (core dumped)" at the end of an install that had
# otherwise finished. A phase you can name is not a nicety for an installer that takes an hour.
elif [ "$1" = "download" ]; then
	setDevices
	download-setup
elif [ "$1" = "build" ]; then
	setDevices
	buildGentoo
elif [ "$1" = "install" ]; then
	setDevices
	download-setup
	buildGentoo
elif [ "$1" = "btrfs-tweaks" ]; then
	btrfsTweaks
elif [ "$1" = "btrfs-tweaks-rewrite" ]; then
	nodatacowRewrite "$2"
elif [ "$1" = "install-flatpaks" ]; then
	installFlatpaks
elif [ "$1" = "compile-kernel" ]; then
	compile-kernel
elif [ "$1" = "repo" ]; then
	# On a running PosterChanOS installation the target is `/`. The script-wide default is the
	# installer staging tree, /tmp/install; leaving that default here made the advertised repair
	# command write into an unused directory and report success while Portage stayed broken.
	export TARGET=/
	gentooRepo
	/usr/bin/emerge --sync
elif [ "$1" = "remove-snapshot" ]; then
	remove-snapshots
elif [ "$1" = "fix-base" ]; then
	fixBase
elif [ "$1" = "fix-build-boot" ]; then
	fix-build-boot
elif [ "$1" = "portage" ]; then
	export TARGET=/
	configurePortage
	unmaskPackages
elif [ "$1" = "fstab" ]; then
	setDevices
	TARGET=/
	fstab
elif [ "$1" = "install-live" ]; then
	liveISOinstall
elif [ "$1" = "livecd" ]; then
	# Scriptable: PC_ISO_OUT / PC_ISO_HOME / PC_ISO_CLEAN answer the three questions, and an
	# unanswered PC_ISO_CLEAN means CLEAN -- see liveCD.
	liveCD
elif [ "$1" = "help" ]; then
	show-help
else
	menu
fi
