#!/usr/bin/bash
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
# For new disk installs, initialize the disk to setup partitions from the main menu.
#
# Before running the install, ensure that you have Internet access.
#
# Please be sure to change USER,USER_PASSWORD, DISK_PASSWORD, and ROOT_PASSWORD strings in this file
#
# To install a new OS to a disk, run gentoo.sh and choose option 5 from the main menu
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

COLOR_CYAN="\033[1;36m"; COLOR_MAGENTA="\033[1;35m"; COLOR_YELLOW="\033[1;33m"
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
SERVICES+=(sshd systemd-timesyncd libvirtd smartd cups NetworkManager boot-snapshot.timer)
MAKEOPTS="-j$(cat /proc/cpuinfo | grep -i processor | grep -vi 'model' | wc -l)"
ROOT_PARTITION_SIZE="30GB"
FEATURES="-pid-sandbox getbinpkg -binpkg-request-signature"
EMERGE_DEFAULT_OPTS="--jobs 5 --getbinpkg "
#USEFLAG CONFIGURATION
# zstd is GLOBAL, not per-package: the live CD build compresses its squashfs with it and
# dracut needs to be able to read that back, so a kernel/initramfs built without the flag
# fails at "zstd is not supported" — after the whole image has been built.
USE_FLAGS=" flatpak dracut -webp -ladspa npm introspection lame systemd-boot dist-kernel luks cryptsetup kernel-install boot opus theora vpx kernel-install systemd firmware btrfs networkmanager zstd"
VIDEO_CARDS="intel amdgpu radeon radeonsi"
#
#PACKAGE CONFIGURATION
BASE_PACKAGES="net-print/cups-filters net-misc/networkmanager net-fs/sshfs app-shells/starship dev-util/sh sys-boot/plymouth sys-power/acpid app-arch/zip dev-python/virtualenv sys-apps/flatpak sys-power/powertop app-shells/bash-completion sys-power/cpupower media-libs/gexiv2 mail-mta/postfix app-admin/sysstat sys-apps/smartmontools net-fs/nfs-utils net-firewall/nftables dev-python/pip sys-fs/inotify-tools net-analyzer/nmap app-misc/screen app-portage/gentoolkit sys-fs/dosfstools app-admin/sudo sys-apps/systemd app-eselect/eselect-repository dev-vcs/git sys-block/parted sys-process/btop net-vpn/wireguard-tools app-editors/neovim app-misc/fastfetch sys-fs/btrfs-progs net-print/cups sys-firmware/seabios-bin sys-firmware/edk2-bin app-emulation/libvirt app-emulation/qemu"
SPECIAL_PACKAGE_USE=("kde-apps/kio-extras samba mtp" "app-db/postgresql icu lz4 nls pam readline server ssl system zlib zstd uuid" "dev-build/meson test test-full" "dev-qt/qtwebengine bindist" "media-sound/sox -opus" "media-video/vlc -opus -theora -vpx" "dev-qt/qtpositioning geoclue" "media-libs/libvpx postproc" "dev-python/pillow webp" "gui-libs/gtk colord sysprof" "media-libs/freetype harfbuzz" "dev-lang/php gmp sodium sysvipc calendar bcmath exif bzip2 intl ctype curl fileinfo filter gd iconv ssl posix session simplexml xmlreader xmlwriter zip zlib postgres png opcache jit cli fpm zip pdo" "net-im/synapse postgres" "net-p2p/qbittorrent webui" "app-crypt/certbot certbot-nginx" "acct-user/git gitea" "app-admin/vaultwarden web postgres" "media-gfx/imagemagick -postscript" "media-gfx/imagemagick -postscript dev-libs/jemalloc statsv" "media-libs/libsdl2 -kms -pipewire" "media-video/obs-studio pipewire wayland" "media-video/pipewire sound-server" "gui-wm/sway X" "mail-mta/postfix sasl")
#
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
POSTERCHANOS_PACKAGES="gui-wm/sway x11-base/xwayland gui-apps/foot \
gui-apps/wl-clipboard \
gui-apps/grim gui-apps/slurp \
x11-misc/xdg-utils \
media-video/pipewire media-video/wireplumber gui-libs/gtk media-fonts/noto media-fonts/noto-emoji \
www-client/firefox-bin \
sys-apps/xdg-desktop-portal gui-libs/xdg-desktop-portal-wlr sys-apps/xdg-desktop-portal-gtk \
media-video/obs-studio \
sec-keys/openpgp-keys-gentoo-release dev-vcs/git"
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
RSYNC_EXCLUDES=" --exclude=-/var/lib/containers --exclude=/var/lib/containerd --exclude=/var/lib/docker --exclude=/var/lib/flatpak --exclude=/home --exclude=/var/lib/pleroma/uploads --exclude=/var/lib/distfiles --exclude=/var/lib/owncloud --exclude=/etc/disk --exclude=/etc/mtab --exclude=/swap --exclude=@swap --exclude=/mnt --exclude=/snapshots --exclude=/backup --exclude=/raid --exclude=/var/tmp/* --exclude=/tmp/* --exclude=/var/lib/libvirt/* --exclude=/var/cache --exclude=/var/notmpfs --exclude=/var/lib/systemd/coredump/* --exclude=/var/cache/* --exclude=/.snapshots/* --exclude=/sys/* --exclude=/dev/* --exclude=/proc/*"
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
	# webrsync fetches a SIGNED SNAPSHOT TARBALL over https, which the public mirror already carries
	# (gentoo.poster.place/snapshots/portage-latest.tar.xz, with its .gpgsig) — the same endpoint the
	# binhost below already uses, so this needs no new infrastructure. The signature is upstream
	# Gentoo's, mirrored verbatim, and verifying it is what makes fetching a tree over HTTP from
	# somebody's server acceptable at all.
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
	echo "GENTOO_MIRRORS=\"https://gentoo.poster.place\"" >>$TARGET/etc/portage/make.conf
}

snapshots() {
	DATE=$(date +%Y-%m-%d-%H-%M)
	YESTERDAY=$(date +%Y-%m-%d -d "5 days ago")
	partitionDetection
	echo
	echo -e "\033[1;35m◆ CREATING SNAPSHOTS... ◆\033[0m"
	echo
	CURRENT_ROOT=$(cat /proc/cmdline | cut -d '@' -f2 | cut -d ' ' -f1)
	if [[ "$CURRENT_ROOT" == *"snapshot"* ]]; then
		echo -e "\033[1;33mAlready booted in Previous\033[0m"
	else
		echo -e "\033[1;33mRemoving Snapshots older than 5 days\033[0m"
		sudo /usr/bin/btrfs sub del /.snapshots/snapshot-*
		sudo rm -f /boot/loader/entries/snapshot-*
		sudo /usr/bin/btrfs sub snapshot / /.snapshots/snapshot-$DATE
		BOOT_FILES=$(sudo bootctl | grep "Current Entry" | cut -d " " -f3)
		sudo cp -f /boot/loader/entries/$BOOT_FILES /boot/loader/entries/snapshot-$DATE.conf
		sudo sed -i "s/@$ROOT_NAME/@.snapshots\/snapshot-$DATE/i" /boot/loader/entries/snapshot-$DATE.conf
	fi
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

	EFI=$(blkid | grep $HARD_DISK | sort | cut -d ":" -f1 | head -1 | tail -1)
	BTRFS=$(blkid | grep $HARD_DISK | sort | cut -d ":" -f1 | head -2 | tail -1)
	ROOT_MAPPER_NAME="/dev/mapper/luks-$(/sbin/blkid -s UUID -o value ${BTRFS})"

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
	dd if=/dev/urandom of=/boot/$KEYFILE bs=1024 count=4
	chown root:root /boot/$KEYFILE
	chmod 0400 /boot/$KEYFILE
	echo
	echo -e "\033[1;33mAdding new key......\033[0m"
	echo
	printf "$DISK_PASSWORD" | cryptsetup luksAddKey $1 /boot/$KEYFILE
	echo "install_items+=\" /boot/unlock.sh /boot/$KEYFILE \"" >>/etc/dracut.conf
	echo "omit_drivers+=\" nouveau \"" >>/etc/dracut.conf

	sed -i "s/none/\/boot\/$KEYFILE/" /etc/crypttab
	echo "#!/bin/bash" >/boot/unlock.sh
	echo "systemd-cryptsetup attach $(echo $ROOT_MAPPER_NAME | grep luks | cut -d '/' -f4)  UUID=$(/sbin/blkid -s UUID -o value ${BTRFS}) /boot/$KEYFILE " >>/boot/unlock.sh
	chmod +x /boot/unlock.sh
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
		partitions
		echo -e "\033[1;33mBTRFS device found\033[0m"
		echo
		echo -e "\033[1;33mMounting Boot,EFI,HOME\033[0m"
		echo
		mount $ROOT_MAPPER_NAME $TARGET
		btrfs_filesytem
		mkdir -p $TARGET/boot/EFI
		mount $EFI $TARGET/boot
		mkdir -p $TARGET/swap
		#CONFIGURE DATA DIRS (HOME)
		mkdir $TARGET/home
		mkdir $TARGET/.snapshots
		mount -o subvol=@home $ROOT_MAPPER_NAME $TARGET/home
		mount -o subvol=@swap $ROOT_MAPPER_NAME $TARGET/swap
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
	GENTOO_PROFILE=$(chroot $TARGET /usr/bin/eselect profile list | grep -i 'desktop' | grep -vi 'plasma\|gnome' | grep systemd | grep -i stable | head -1 | cut -d '[' -f2 | cut -d ']' -f1)
	chroot $TARGET /usr/bin/eselect profile set $GENTOO_PROFILE

	mkdir -p $TARGET/etc/portage/package.license
	echo "*/*  *" >$TARGET/etc/portage/package.license/license
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
	cp -f gentoo.sh $TARGET/usr/bin/gentoo.sh
	chroot $TARGET /usr/bin/bash /usr/bin/gentoo.sh install-packages
	echo
	echo
	echo -e "\033[1;36m[Configuring Accounts and post-setup tasks]\033[0m"
	echo
	echo
	finalizeInstall
}

finalizeInstall() {
	echo 'bash /usr/bin/gentoo.sh bootloader' >>$TARGET/setup.sh
	echo 'bash /usr/bin/gentoo.sh accounts' >>$TARGET/setup.sh
	echo 'bash /usr/bin/gentoo.sh services' >>$TARGET/setup.sh
	echo "chown -R $USER:$USER /home/$USER" >>$TARGET/setup.sh
	# THE DISPLAY MANAGER IS A KDE COMPONENT AND PosterChanOS DOES NOT HAVE ONE. Enabling a unit
	# that was never installed fails the whole finalize step — and on the profile whose entire point
	# is that the shell IS the desktop, there is nothing for a login screen to launch. The shell
	# session (autologin into sway, which starts PosterChan) goes in instead.
	touch $TARGET/etc/posterchanos
	# THE MACHINE CALLS ITSELF WHAT IT IS. Without this the installed system answers "Gentoo"
	# to everything that asks — the login banner, hostnamectl, neofetch, the bootloader entry,
	# every crash report — on an operating system whose whole point is that it is PosterChanOS.
	# The branding was already right in every string a person reads INSIDE the shell, which is
	# exactly why the gap was easy to miss: it is only visible from outside it.
	#
	# `ID` is lowercase because the spec says so (os-release IDs are lowercase, no spaces), and
	# `ID_LIKE=gentoo` is load-bearing: it is how portage tooling, bug reporters and anything
	# reading os-release keep treating this as the Gentoo it actually is. `NAME`/`PRETTY_NAME`
	# are the display strings, and those get the real capitalisation.
	cat >$TARGET/etc/os-release <<-'OSREL'
		NAME="PosterChanOS"
		PRETTY_NAME="PosterChanOS"
		ID=posterchanos
		ID_LIKE=gentoo
		ANSI_COLOR="1;36"
		HOME_URL="https://poster.place/"
	OSREL
	# Gentoo ships os-release as a symlink into /usr/lib; the heredoc above would otherwise
	# rewrite the file it points at, so the distro's own copy stays intact underneath.
	ln -sf ../etc/os-release $TARGET/usr/lib/os-release 2>/dev/null || true
	cp -f gentoo.sh $TARGET/usr/bin/gentoo.sh
	chroot $TARGET /usr/bin/bash /usr/bin/gentoo.sh posterchan-shell
	plymouthTheme
	chmod +x $TARGET/usr/bin/gentoo.sh
	chmod +x $TARGET/setup.sh
	cp -f /tmp/disk $TARGET/etc/disk
	chroot $TARGET /setup.sh
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
# own equivalents. The remote stays because Steam is installed through it (see the steam function),
# and because it is the sane place for a person to get an app this OS does not ship.
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
	SCRIPT=$(pwd)
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
		sudo rsync -avz --delete --rsync-path='sudo rsync' / $RSYNC_EXCLUDES $TARGET/
		sudo rsync -avz --delete --rsync-path='sudo rsync' /boot/ $TARGET/boot/
	fi

	fstab
	cp -f $SCRIPT/gentoo.sh $TARGET/usr/bin/
	cp -f /tmp/disk $TARGET/etc/

	finalizeInstall
	cd
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
	btrfs sub create $TARGET/@$ROOT_NAME
	btrfs sub create $TARGET/@.snapshots
	btrfs sub create $TARGET/@libvirt
	btrfs sub create $TARGET/@home
	btrfs sub create $TARGET/@root
	btrfs sub create $TARGET/@swap
	if [ -z "${SWAP_SIZE}" ]; then
		btrfs filesystem mkswapfile --size "$(free -m | awk '{print $2}' | tail -2 | head -1)m" $TARGET/@swap/swap
	else
		btrfs filesystem mkswapfile --size "$SWAP_SIZE" $TARGET/@swap/swap
	fi
	echo
	echo -e "\033[1;33mBinding BTRFS Root\033[0m"
	echo
	umount $TARGET
	mount -o $COMPRESSION,subvol=@$ROOT_NAME $ROOT_MAPPER_NAME $TARGET
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

	echo "[Service]" > /etc/systemd/system/boot-snapshot.service
	echo "ExecStart=/usr/bin/gentoo.sh snapshot" >> /etc/systemd/system/boot-snapshot.service
	echo "User=verita84"  >> /etc/systemd/system/boot-snapshot.service
	echo "Group=verita84"  >> /etc/systemd/system/boot-snapshot.service
	echo "SyslogIdentifier=boot-snapshot"  >> /etc/systemd/system/boot-snapshot.service
	echo "[Install]"  >> /etc/systemd/system/boot-snapshot.service
	echo "WantedBy=default.target"  >> /etc/systemd/system/boot-snapshot.service

    echo "[Unit]" > /etc/systemd/system/boot-snapshot.timer     
	echo "Description=Boot Snapshots" >> /etc/systemd/system/boot-snapshot.timer
	echo "[Timer]" >> /etc/systemd/system/boot-snapshot.timer
	echo "OnBootSec=0" >> /etc/systemd/system/boot-snapshot.timer
	echo "Unit=boot-snapshot.service" >> /etc/systemd/system/boot-snapshot.timer
	echo "[Install]" >> /etc/systemd/system/boot-snapshot.timer
	echo "WantedBy=default.target" >> /etc/systemd/system/boot-snapshot.timer

	for i in "${SERVICES[@]}"; do
		systemctl enable $i
	done
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
	# `plymouth-set-default-theme -R` rebuilds the initramfs, which is the half that is actually
	# load-bearing: the theme lives INSIDE the initramfs at boot, so a theme set without a rebuild
	# is a theme that will not appear and gives no hint as to why.
	if [ -n "$TARGET" ] && [ "$TARGET" != "/" ]; then
		chroot $TARGET /usr/bin/plymouth-set-default-theme -R posterchanos || \
			chroot $TARGET /usr/bin/plymouth-set-default-theme posterchanos
	else
		plymouth-set-default-theme -R posterchanos || plymouth-set-default-theme posterchanos
	fi
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

	# THE COMPOSITOR DRAWS NO CHROME, because PosterChan draws it. Left on, sway's own borders and
	# title bars would sit on top of the PosterChan desktop — two window styles on one screen, and
	# the native one wearing the wrong font. PosterChan already knows the rectangle it assigned each
	# window, so it renders its title bar and border AROUND that rectangle and insets the native
	# window inside it; the frame is never covered, so drags and window buttons land on the same
	# os.js code that moves an HTML window. One style for Notes and for Firefox.
	default_border none
	default_floating_border none
	gaps inner 0
	gaps outer 0

	# Nothing draws over the desktop uninvited — no compositor wallpaper, no status bar. PosterChan
	# is the wallpaper and the taskbar.
	output * bg #000000 solid_color

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
	bindsym $mod+Return exec foot

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
	bindsym --release --no-repeat $mod exec swaymsg -t send_tick pc:start

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
			return 0
		fi
	fi

	# THE SHELL ITSELF. sway's config execs `posterchan`, and nothing else here installs it — so
	# without this the machine boots into an empty compositor with no way to do anything, which is
	# the most convincing possible imitation of a broken install.
	#
	# The AppImage is EXTRACTED rather than run as one. An AppImage needs FUSE at runtime, and FUSE
	# is exactly the sort of thing a minimal profile does not have; extracting once at install time
	# needs it never, and turns the shell into an ordinary directory of files that starts in the
	# time it takes to exec.
	echo -e "\033[1;33mInstalling the PosterChan desktop\033[0m"
	APPIMG="/tmp/PosterChan.AppImage"
	mkdir -p ${TARGET}/tmp 2>/dev/null
	if [ ! -f "$APPIMG" ]; then
		curl -sSfL --retry 3 --connect-timeout 20 -o "$APPIMG" \
			https://github.com/loblawbob873-svg/posterchanai/releases/download/desktop-latest/PosterChan.AppImage \
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
	if [ -s "$APPIMG" ]; then
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
		_in 'printf "%s\n" "#!/bin/sh" \
			"# The extracted AppImage has no runtime to set APPDIR, and AppRun needs it." \
			"export APPDIR=/opt/posterchan" \
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
			echo -e "\033[1;31m  ✗ the AppImage did not extract — sway will start with no shell\033[0m"
		fi
	else
		echo -e "\033[1;31m  ✗ could not download the PosterChan desktop — sway will start with no shell\033[0m"
	fi

	# ANYONE MAY SIGN IN, so an account has to exist before they have anywhere to put anything.
	# PosterChanOS logs in with a KEY; home directories and permissions are a Unix idea, and this is
	# what joins the two. It is the ONLY privileged thing the shell asks for, and it is limited to
	# exactly that one command — signing in with a key is not the same as being trusted with root,
	# and a machine anyone may log into must not hand every visitor sudo.
	for helper in pc-provision-user pc-shell-start pc-key; do
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

	# Autologin straight into the shell. A display manager is another package, another theme and
	# another thing between the power button and the desktop.
	GETTY_DIR="${TARGET}/etc/systemd/system/getty@tty1.service.d"
	mkdir -p $GETTY_DIR
	printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin %s --noclear %%I $TERM\n' \
		"$SHELL_USER" >$GETTY_DIR/override.conf

	# ...and start sway from the login shell on tty1 only, so a second console is still a console.
	mkdir -p ${TARGET}/home/$SHELL_USER
	cat >${TARGET}/home/$SHELL_USER/.bash_profile <<-'PROFILE'
	[[ -f ~/.bashrc ]] && . ~/.bashrc
	if [ -z "$WAYLAND_DISPLAY" ] && [ "$XDG_VTNR" = 1 ]; then
		export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=sway MOZ_ENABLE_WAYLAND=1
		exec sway
	fi
	PROFILE
	chown $SHELL_USER:$SHELL_USER ${TARGET}/home/$SHELL_USER/.bash_profile 2>/dev/null
}

installSteam() {
	# Steam is OPT-IN and stays that way — a separate step, exactly as it was. gamescope belongs
	# here rather than in the base package list: it is a micro-compositor for GAMES, useless on a
	# machine that never installs Steam, and it has no business being emerged on one. (It also does
	# not live where its name suggests — `gui-wm/gamescope`, not `games-util/` — and one
	# unresolvable atom makes emerge refuse the whole set it appears in.)
	# THE 32-BIT STACK IS THE WHOLE COST, and on a source distribution it is measured in hours.
	# Native steam-launcher pulls ABI_X86=32 through the entire graphics stack — every one of those
	# libraries built twice — for a program that ships its own runtime anyway. So on PosterChanOS
	# Steam comes as a FLATPAK, which is what this script always did before the minimal profile
	# dropped flatpak: one prebuilt download instead of a multilib world rebuild, and the base
	# system stays free of a 32-bit ABI it has no other use for.
	#
	# gamescope is emerged natively either way: it is 64-bit only, small, and it is what lets a game
	# have the screen to itself under the compositor.
	emerge -uDN sys-apps/flatpak gui-wm/gamescope --autounmask-write
	etc-update -q --automode -5
	emerge -uDN sys-apps/flatpak gui-wm/gamescope
	/usr/bin/flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
	/usr/bin/flatpak install -y com.valvesoftware.Steam
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
	for g in audio video input netdev render; do
		getent group "$g" >/dev/null 2>&1 && gpasswd -a $SHELL_USER "$g" >/dev/null 2>&1
	done
	# One command, not ALL. The shell provisions accounts; it is not an administrator.
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

initializeDisk() {
	clear
	echo
	echo -e "\033[1;36m[PosterChanOS Installer - Initialize Device]\033[0m"
	echo
	read -p 'Proceed with Wiping the disk? (y/n): ' -i "local" choice
	if [[ $choice = *y* ]]; then
		parted /dev/$HARD_DISK mklabel gpt
		parted -a optimal /dev/$HARD_DISK mkpart primary fat32 1MiB 2024MiB
		parted -a optimal /dev/$HARD_DISK set 1 esp on
		parted -a optimal /dev/$HARD_DISK mkpart P2 ext3 2024MiB 100%

		partitionDetection
		printf "$DISK_PASSWORD\n$DISK_PASSWORD" | cryptsetup luksFormat ${BTRFS}
		printf "$DISK_PASSWORD" | cryptsetup open ${BTRFS} $(echo $ROOT_MAPPER_NAME | sed 's/\/dev\/mapper\///')

		echo
		echo -e "\033[1;33mFormatting.....\033[0m"
		echo -e "\033[1;33mmkfs.btrfs $ROOT_MAPPER_NAME -f\033[0m"
		echo y | mkfs.btrfs $ROOT_MAPPER_NAME -f
		echo
		echo -e "\033[1;33mFormatting $EFI\033[0m"
		echo
		echo y | mkfs.vfat $EFI

		echo -e "\033[1;33mInitialize Complete. Please reboot your machine to avoid any issues\033[0m"
		echo
		cryptsetup close $ROOT_MAPPER_NAME
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
	else
		tweaks
	fi
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
	if ! mksquashfs -help 2>&1 | grep -qw zstd; then
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
		if ! mksquashfs -help 2>&1 | grep -qw zstd; then
			_lcd_fail "It rebuilt and still has no zstd. Check USE for sys-fs/squashfs-tools."
			return
		fi
	fi

	# ---------------------------------------------------------------- where
	#
	# WHERE IT LANDS IS DECIDED HERE AND SAID OUT LOUD, because an answer that quietly becomes
	# somewhere else costs the whole build. "the iso is saving to ~": an empty answer, or a relative
	# one, resolves against whatever directory the script was started from — usually the home
	# directory of whoever ran it — and a multi-gigabyte image plus its work tree lands on the
	# partition least able to take it, silently.
	local OUTDIR ISO WORK LABEL DEFOUT
	DEFOUT="/var/tmp/livecd"
	read -p 'Write the ISO where? ' -e -i "$DEFOUT" OUTDIR
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
	read -p 'Include /home in the image? ' -e -i "n" KEEP_HOME

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
	local CLEAN
	read -p "Clean out this machine's accounts and secrets (n = personal rescue disc)? " -e -i "y" CLEAN

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
		var/log/journal .snapshots
		boot efi
		etc/fstab etc/machine-id etc/crypttab
	)
	# THE WORK DIRECTORY EXCLUDES ITSELF — see the header. Stored relative, because mksquashfs's
	# -e paths are relative to the source root.
	EXCLUDES+=("${OUTDIR#/}")
	# A CLEAN IMAGE HAS NOBODY'S HOME IN IT, whatever was answered above. The two questions can be
	# answered in contradiction — strip the accounts, keep the home directories — and honouring both
	# literally would ship somebody's files under a user that no longer exists to own them, readable
	# by uid 1000, which is `live`.
	[[ "$KEEP_HOME" = *n* || "$CLEAN" = *y* ]] && EXCLUDES+=(home)
	local f
	for f in $SWAPFILES; do
		[[ -n "$f" ]] && EXCLUDES+=("${f#/}")
	done

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

		# The account files, rewritten. Everything below uid 1000 stays — root and the system users
		# are what makes a Linux system work — and every real person is dropped, replaced by one
		# passwordless `live`.
		awk -F: '$3 < 1000 || $3 >= 65534' /etc/passwd  >"$WORK/passwd"
		echo 'live:x:1000:1000:Live session:/home/live:/bin/bash' >>"$WORK/passwd"
		awk -F: '$3 < 1000 || $3 >= 65534' /etc/group   >"$WORK/group"
		echo 'live:x:1000:' >>"$WORK/group"
		# EMPTY password field, not a hash and not `!`. Empty is "no password"; `!` is "locked", and
		# a locked account cannot autologin — which would be a live disc that boots to a prompt
		# nobody has the answer to.
		awk -F: 'NR==FNR { if ($3 >= 1000 && $3 < 65534) drop[$1]; next } !($1 in drop)' \
			/etc/passwd /etc/shadow >"$WORK/shadow" 2>/dev/null || cp /etc/shadow "$WORK/shadow"
		echo 'live::20000:0:99999:7:::' >>"$WORK/shadow"
		# The groups that decide whether a desktop can use the hardware. Taken from what THIS machine
		# actually has rather than a guessed list, because a live user outside `video`/`input` gets a
		# desktop with no screen and no keyboard.
		local G
		for G in wheel video input audio render seat plugdev users; do
			grep -q "^$G:" /etc/group && sed -i "s/^\($G:[^:]*:[^:]*:\)\(.*\)$/\1\2,live/; s/,live,live/,live/; s/:,live$/:live/" "$WORK/group"
		done
		# Autologin as the live user. Same file the installed system uses, rewritten rather than
		# removed — deleting it gives a login prompt for an account with no password set.
		mkdir -p "$WORK/gettyd"
		printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin live --noclear %%I $TERM\n' \
			>"$WORK/gettyd/override.conf"
	fi

	local PSEUDO="$WORK/pseudo"
	{
		echo "etc/fstab f 644 0 0 cat $LIVEFSTAB"
		# An EMPTY machine-id, not a copy of this machine's. systemd treats empty as "first boot"
		# and generates a fresh one; a duplicated id gives every live boot the same identity, which
		# breaks journald, DHCP leases and systemd-boot's own /boot layout.
		echo "etc/machine-id f 444 0 0 echo -n"

		if [[ "$CLEAN" = *y* ]]; then
			echo "etc/passwd f 644 0 0 cat $WORK/passwd"
			echo "etc/group f 644 0 0 cat $WORK/group"
			echo "etc/shadow f 640 0 0 cat $WORK/shadow"
			echo "home d 755 0 0"
			echo "home/live d 755 1000 1000"
			echo "etc/systemd/system/getty@tty1.service.d d 755 0 0"
			echo "etc/systemd/system/getty@tty1.service.d/override.conf f 644 0 0 cat $WORK/gettyd/override.conf"
			# A hostname that is not yours. `posterchanos` is what an unconfigured install should
			# call itself, and it is what the installer changes.
			echo "etc/hostname f 644 0 0 echo posterchanos"
		fi

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
					echo "usr/local/share/posterchanos/$REL f 755 0 0 cat \"$ISRC\""
				fi
			done < <(find "$IHERE" -mindepth 1 \( -type f -o -type d \) | sort)

			# AND A WAY TO FIND IT. A terminal command nobody is told about is not a way to install
			# an operating system. The desktop's start menu lists every .desktop file on the machine
			# (see `_machineApps`), so an entry here puts "Install PosterChanOS" in the menu of the
			# live session with no extra wiring — the same list Firefox and the rest come from.
			echo "usr/share/applications d 755 0 0"
			echo "usr/share/applications/posterchanos-install.desktop f 644 0 0 cat $WORK/install.desktop"
		fi
	} >"$PSEUDO"

	cat >"$WORK/install.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Install PosterChanOS
Comment=Install this system onto a disk
Icon=drive-harddisk
Exec=foot -T "Install PosterChanOS" -e sh -c 'if [ "$(id -u)" = 0 ]; then /usr/local/share/posterchanos/gentoo.sh; else sudo /usr/local/share/posterchanos/gentoo.sh; fi'
Terminal=false
Categories=System;
DESKTOP

	# ---------------------------------------------------------------- squash it
	echo -e "${COLOR_YELLOW}Packing the filesystem — this is the slow part.${COLOR_RESET}"
	echo
	local EXARGS=()
	for f in "${EXCLUDES[@]}"; do EXARGS+=(-e "$f"); done
	if ! mksquashfs / "$WORK/iso/LiveOS/squashfs.img" \
		-comp zstd -Xcompression-level 15 -noappend -no-progress \
		-pf "$PSEUDO" "${EXARGS[@]}"; then
		echo
		_lcd_fail "mksquashfs failed — nothing was written."
		return
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
	# 4. ANY kernel, newest — and say so, because booting the live image on a kernel whose modules
	#    are not in the image is a different failure and the person should know which one they have.
	if [[ -z "$KERNEL" ]]; then
		KERNEL="$(ls -1 /boot/*/*/linux /boot/vmlinuz* /boot/linux-* 2>/dev/null | sort -V | tail -1)"
		[[ -n "$KERNEL" ]] && echo -e "${COLOR_YELLOW}The running kernel ($KVER) is not under /boot; using $KERNEL instead.${COLOR_RESET}"
	fi
	if [[ -z "$KERNEL" ]]; then
		{ echo "no kernel; /boot holds:"; ls -1 /boot 2>/dev/null | head -40; } >>"$LOG" 2>/dev/null
		ls -1 /boot 2>/dev/null | head -20
		_lcd_fail "No kernel found under /boot — looked for /boot/<machine-id>/$KVER/linux, /boot/vmlinuz-$KVER* and /lib/modules/$KVER/vmlinuz."
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
	if ! dracut --force --no-hostonly --nolvmconf --nomdadmconf \
		--add "dmsquash-live" --omit "crypt crypt-gpg crypt-loop" \
		--kver "$KVER" "$WORK/iso/boot/initramfs.img"; then
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
	grub-mkrescue -o "$ISO" "$WORK/iso" -- -volid "$LABEL" 2>&1 | tee -a "$LOG"
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
	read -p "Press enter key to Continue"
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
		i=0
		while [ $i != "n" ]; do
			clear
			echo
			echo -e "\033[1;33mDisks and Partitions:\033[0m"
			echo
			cat /proc/partitions
			echo
			echo -e "\033[1;33mErase the line and press enter to skip to the next detected disk\033[0m"
			echo
			i=$(expr $i + 1)
			read -p 'Disk Device to Use: ' -e -i $(lsblk | grep -i disk | grep -Evi 'swap|zram|dm-0' | cut -d ' ' -f1 | head -$i | tail -1) device
			if [[ ! -z $device ]]; then
				i="n"
			fi
		done

		read -p 'BTRFS Root Volume name:  ' -e -i "gentoo" root_name
		read -p 'LUKS Device Mapper Name:  ' -e -i "root" device_mapper_name

		HARD_DISK=$device
		echo $HARD_DISK >/tmp/disk
		echo $root_name >>/tmp/disk
		echo $device_mapper_name >>/tmp/disk
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

bootloader() {
	chmod -R 740 /boot/EFI
	rm -rf /boot/loader/entries/*
	if [ -f "/etc/disk" ]; then
		partitionDetection
		echo
		echo -e "\033[1;33mInstalling Bootloader...................\033[0m"
		sleep 3
		echo
		bootctl install
		MACHINE_ID=$(cat /etc/machine-id)
		KERNEL="kernel-$(ls /boot/$MACHINE_ID | grep gentoo | tail -1)"
		KERNEL_VERSION=$(echo $KERNEL | cut -d '-' -f2-5)
		LOADER_FILE="/boot/loader/entries/$MACHINE_ID-$KERNEL_VERSION.conf"
		PREVIOUS_LOADER_FILE="/boot/loader/entries/previous.conf"
		OFFSET=$(btrfs inspect-internal map-swapfile /swap/swap -r)
		UUID=$(/usr/bin/findmnt -no UUID -T /swap/swap | head -1)

		KERNEL_COMMAND_LINE="options quiet splash usbcore.quirks=0bda:8156,0bda:8153 rd.luks.key=/boot/keyfile.key mitigations=off resume=UUID=$UUID resume_offset=$OFFSET  root=UUID=$(/sbin/blkid -s UUID -o value $ROOT_MAPPER_NAME) rootflags=subvol=@$ROOT_NAME rw "
		rm -f /etc/crypttab
		echo >/etc/dracut.conf
		mkdir -p /boot/$MACHINE_ID/$KERNEL_VERSION

		KERNEL_COMMAND_LINE="$KERNEL_COMMAND_LINE: rd.luks.uuid=$(/sbin/blkid -s UUID -o value ${BTRFS})"
        dracut_modules=" crypt systemd-cryptsetup dm rootfs-block "
        echo "add_dracutmodules+=\" $dracut_modules \"" >> /etc/dracut.conf  
		echo "kernel_cmdline+=\" $KERNEL_COMMAND_LINE \" " >>/etc/dracut.conf

		echo "$(echo $ROOT_MAPPER_NAME | sed 's/\/dev\/mapper\///') UUID=$(/sbin/blkid -s UUID -o value ${BTRFS})  none luks" >/etc/crypttab

		if [ "$AUTO_DECRYPT" == "True" ]; then
			decryptBoot "$BTRFS"
		fi

        echo -e "\033[1;33mDeleting old Kernel Modules\033[0m"
        echo
        cd /usr/lib/modules
        ls /usr/lib/modules | grep -Evi $KERNEL_VERSION | xargs rm -r
		dracut --regenerate-all -f
		mkdir -p /boot/$MACHINE_ID/$KERNEL_VERSION
		plymouth-set-default-theme solar

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
	bootloader
elif [ "$1" = "snapshot" ]; then
	snapshots
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
	gentooRepo
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
elif [ "$1" = "help" ]; then
	show-help
else
	menu
fi
