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
USE_FLAGS=" flatpak dracut -webp -ladspa npm introspection lame systemd-boot dist-kernel luks cryptsetup kernel-install boot opus theora vpx kernel-install systemd firmware btrfs networkmanager"
VIDEO_CARDS="intel amdgpu radeon radeonsi"
#
#PACKAGE CONFIGURATION
BASE_PACKAGES="net-print/cups-filters net-misc/networkmanager net-fs/sshfs app-shells/starship dev-util/sh sys-boot/plymouth sys-power/acpid app-arch/zip dev-python/virtualenv sys-apps/flatpak sys-power/powertop app-shells/bash-completion sys-power/cpupower media-libs/gexiv2 mail-mta/postfix app-admin/sysstat sys-apps/smartmontools net-fs/nfs-utils net-firewall/nftables dev-python/pip sys-fs/inotify-tools net-analyzer/nmap app-misc/screen app-portage/gentoolkit sys-fs/dosfstools app-admin/sudo sys-apps/systemd app-eselect/eselect-repository dev-vcs/git sys-block/parted sys-process/btop net-vpn/wireguard-tools app-editors/neovim app-misc/fastfetch sys-fs/btrfs-progs net-print/cups sys-firmware/seabios-bin sys-firmware/edk2-bin app-emulation/libvirt app-emulation/qemu"
DESKTOP_APPS=" media-sound/elisa kde-apps/kcalc media-video/obs-studio media-video/vlc kde-apps/kdenlive app-editors/vscodium kde-apps/dolphin kde-apps/konsole firefox-bin net-im/telegram-desktop-bin media-fonts/noto media-fonts/noto-emoji app-emulation/virt-manager net-wireless/bluez sys-power/power-profiles-daemon kde-plasma/discover media-fonts/fontawesome kde-plasma/plasma-meta "
SPECIAL_PACKAGE_USE=("kde-apps/kio-extras samba mtp" "app-db/postgresql icu lz4 nls pam readline server ssl system zlib zstd uuid" "dev-build/meson test test-full" "dev-qt/qtwebengine bindist" "media-sound/sox -opus" "media-video/vlc -opus -theora -vpx" "dev-qt/qtpositioning geoclue" "media-libs/libvpx postproc" "dev-python/pillow webp" "gui-libs/gtk colord sysprof" "media-libs/freetype harfbuzz" "dev-lang/php gmp sodium sysvipc calendar bcmath exif bzip2 intl ctype curl fileinfo filter gd iconv ssl posix session simplexml xmlreader xmlwriter zip zlib postgres png opcache jit cli fpm zip pdo" "net-im/synapse postgres" "net-p2p/qbittorrent webui" "app-crypt/certbot certbot-nginx" "acct-user/git gitea" "app-admin/vaultwarden web postgres" "media-gfx/imagemagick -postscript" "media-gfx/imagemagick -postscript dev-libs/jemalloc statsv" "media-libs/libsdl2 -kms -pipewire" "media-video/obs-studio pipewire wayland" "media-video/pipewire sound-server" "gui-wm/sway X" "mail-mta/postfix sasl")
FLATPAK_PACKAGES="com.valvesoftware.Steam com.vscodium.codium org.kde.konsole com.brave.Browser org.mozilla.Thunderbird net.cozic.joplin_desktop io.github.martchus.syncthingtray im.riot.Riot org.telegram.desktop org.kde.krita org.remmina.Remmina org.onlyoffice.desktopeditors org.kde.kdenlive org.kde.kcalc com.obsproject.Studio com.bitwarden.desktop org.vinegarhq.Sober org.videolan.VLC org.kde.dolphin"
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
POSTERCHANOS_PACKAGES="gui-wm/sway x11-base/xwayland gui-apps/foot gui-apps/wl-clipboard \
media-video/pipewire media-video/wireplumber gui-libs/gtk media-fonts/noto media-fonts/noto-emoji \
www-client/firefox-bin \
sys-apps/xdg-desktop-portal gui-libs/xdg-desktop-portal-wlr sys-apps/xdg-desktop-portal-gtk \
media-video/obs-studio \
sec-keys/openpgp-keys-gentoo-release dev-vcs/git"

# The profile has to survive a CHROOT. buildGentoo copies this script into the target and runs it
# there for the package step, and an environment variable does not cross that boundary — so the
# choice is a FILE, written into the target once, and read here on every invocation. Without it the
# chroot run silently rebuilds the KDE package list and installs a second desktop.
POSTERCHANOS="${POSTERCHANOS:-n}"
[ -f /etc/posterchanos ] && POSTERCHANOS="y"

PACKAGES="$BASE_PACKAGES $DESKTOP_APPS"
if [[ "$POSTERCHANOS" = *y* ]]; then
	PACKAGES="$BASE_PACKAGES $POSTERCHANOS_PACKAGES"
	FLATPAK_PACKAGES=""
fi
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

	# `emerge --sync` MUST WORK ON A MACHINE THAT IS NOT ON THIS LAN.
	#
	# The default here syncs from rsync://gentoo-repo.lan, which resolves for exactly one network —
	# so a PosterChanOS install anywhere else has a broken --sync from first boot, and the way you
	# find out is that the machine can never update. An OS somebody else runs cannot be pointed at a
	# .lan name.
	#
	# webrsync fetches a SIGNED SNAPSHOT TARBALL over https, which the public mirror already carries
	# (gentoo.poster.place/snapshots/portage-latest.tar.xz, with its .gpgsig) — the same endpoint the
	# binhost above already uses, so this needs no new infrastructure. The signature is upstream
	# Gentoo's, mirrored verbatim, and verifying it is what makes fetching a tree over HTTP from
	# somebody's server acceptable at all.
	{
		echo "[gentoo]"
		echo "location = /var/db/repos/gentoo"
		if [[ "$POSTERCHANOS" = *y* ]]; then
			echo "sync-type = webrsync"
			echo "sync-uri = https://gentoo.poster.place"
			echo "sync-webrsync-verify-signature = true"
		else
			echo "sync-type = rsync"
			echo "sync-uri = rsync://gentoo-repo.lan/gentoo-portage"
		fi
	} >$TARGET/etc/portage/repos.conf/gentoo-mirror.conf

	# THE POSTERCHANOS OVERLAY: how an installed machine gets a newer desktop and session without
	# being reinstalled. A git repo rather than a directory of files, because that is the only shape
	# portage can sync over plain https.
	if [[ "$POSTERCHANOS" = *y* ]]; then
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
	fi

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
	if [[ "$POSTERCHANOS" = *y* ]]; then
		GENTOO_PROFILE=$(chroot $TARGET /usr/bin/eselect profile list | grep -i 'desktop' | grep -vi 'plasma\|gnome' | grep systemd | grep -i stable | head -1 | cut -d '[' -f2 | cut -d ']' -f1)
	else
		GENTOO_PROFILE=$(chroot $TARGET /usr/bin/eselect profile list | grep -i 'plasma' | grep systemd | grep -i stable | head -1 | cut -d '[' -f2 | cut -d ']' -f1)
	fi
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
	if [[ "$POSTERCHANOS" = *y* ]]; then touch $TARGET/etc/posterchanos; fi
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
	if [[ "$POSTERCHANOS" = *y* ]]; then
		touch $TARGET/etc/posterchanos
		cp -f gentoo.sh $TARGET/usr/bin/gentoo.sh
		chroot $TARGET /usr/bin/bash /usr/bin/gentoo.sh posterchan-shell
		plymouthTheme
	else
		chroot $TARGET /usr/bin/systemctl enable sddm
	fi
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

installFlatpaks() {
	/usr/bin/flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
	/usr/bin/flatpak install -y $FLATPAK_PACKAGES
}

btrfsTweaks() {
	DISABLE_COW=("/var/lib/postgresql" "/var/lib/mysql" "/var/lib/libvirt")

	for i in "${DISABLE_COW[@]}"; do
		chattr -R +C $i
	done
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
	SRC="$(dirname "$0")/plymouth/posterchanos"
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
	cat >${TARGET}/etc/portage/package.mask/posterchanos <<-'MASK'
	# PosterChanOS: no HTML engine may be built from source on this profile.
	net-libs/webkit-gtk
	net-libs/webkit-gtk-6
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
	if [[ "$POSTERCHANOS" = *y* ]] && [ -f "${TARGET}/etc/portage/repos.conf/posterchan.conf" ]; then
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
		if [ -f "$(dirname "$0")/bin/$helper" ]; then
			cp -f "$(dirname "$0")/bin/$helper" ${TARGET}/usr/local/bin/$helper
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
	if [[ "$POSTERCHANOS" = *y* ]]; then
		emerge -uDN sys-apps/flatpak gui-wm/gamescope --autounmask-write
		etc-update -q --automode -5
		emerge -uDN sys-apps/flatpak gui-wm/gamescope
		/usr/bin/flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
		/usr/bin/flatpak install -y com.valvesoftware.Steam
		return 0
	fi
	eselect repository enable steam-overlay
	emerge --sync steam-overlay
	emerge -uDN games-util/steam-launcher app-emulation/wine-vanilla gui-wm/gamescope --autounmask-write
	etc-update -q --automode -5
	emerge -uDN @world
	emerge -uDN games-util/steam-launcher app-emulation/wine-vanilla gui-wm/gamescope
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
	if [[ "$POSTERCHANOS" = *y* ]]; then
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
		return 0
	fi

	echo
	echo -e "\033[1;33mSet Password for $USER\033[0m"
	useradd -m -d /home/$USER -s /bin/bash $USER
	echo "$USER:$USER_PASSWORD" | chpasswd
	gpasswd -a $USER wheel
	gpasswd -a $USER network
	gpasswd -a $USER video
	gpasswd -a $USER libvirt
	gpasswd -a $USER netdev
	gpasswd -a $USER adm
	gpasswd -a $USER video
	gpasswd -a $USER lp
	gpasswd -a $USER lpadmin
	# THE INCLUDEDIR HAS TO SURVIVE. Writing /etc/sudoers wholesale drops the line that makes
	# /etc/sudoers.d readable at all, so every drop-in rule is silently ignored — including the one
	# that lets the shell provision an account for somebody signing in. Nothing reports it: sudoers.d
	# files that are never read look exactly like sudoers.d files that are.
	echo "$USER ALL=(ALL) NOPASSWD: ALL" >/etc/sudoers
	echo "root ALL=(ALL) ALL" >>/etc/sudoers
	echo "@includedir /etc/sudoers.d" >>/etc/sudoers
	# SUDO REFUSES TO RUN AT ALL IF THIS FILE IS NOT 0440 root:root, and `echo >` CREATES it with
	# the default umask when it does not already exist — which is what happens whenever this runs
	# before app-admin/sudo is installed. The result is a machine with no working sudo and, since
	# root is locked two lines below, no way in at all except editing the kernel command line.
	# Measured on a real install: 0644, and "sudo: no valid sudoers sources found, quitting".
	chown root:root /etc/sudoers
	chmod 0440 /etc/sudoers
	# ...and say so if it is still not valid, rather than discovering it after the reboot.
	if command -v visudo >/dev/null 2>&1; then
		visudo -c >/dev/null 2>&1 || echo -e "\033[1;31m  ✗ /etc/sudoers is not valid — sudo will refuse everything\033[0m"
	fi
	echo
	echo -e "\033[1;33mSetting ROOT Password:\033[0m"
	echo "root:$ROOT_PASSWORD" | chpasswd
	echo -e "\033[1;33mDisabling ROOT Account:\033[0m"
	/usr/bin/passwd -dl root
	/usr/bin/hostnamectl set-hostname $ROOT_NAME
	sed -i 's/#Storage=persistent/Storage=volatile/i' /etc/systemd/journald.conf
	sed -i 's/#ForwardToSyslog=no/ForwardToSyslog=no/i' /etc/systemd/journald.conf
}

btrfs-tweaks() {
	DISABLE_COW=("/var/lib/docker" "/volumes" "/var/lib/mysql" "/var/lib/libvirt")

	for i in "${DISABLE_COW[@]}"; do
		chattr -R +C $i
	done
}

initializeDisk() {
	clear
	echo
	echo -e "\033[1;36m[Gentoo Installer - Initialize Device]\033[0m"
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
	echo -e "\033[1;36m[Gentoo Installer System Tweaks]\033[0m"
	echo
	echo -e "\033[1;36m[1] Chroot into existing OS\033[0m"
	echo -e "\033[1;36m[2] Enable/Disable Disk Password at Boot\033[0m"
	echo -e "\033[1;36m[3] Compile the Kernel\033[0m"
	echo -e "\033[1;36m[4] Upgrade gentoo.sh\033[0m"
	echo -e "\033[1;36m[5] Fix Audio\033[0m"
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
		rm -f gentoo.sh
		rm -f repos.conf
		rm -f gentoobinhost.conf
		rm -f /tmp/latest-stage3-amd64-desktop-systemd.txt
		#wget https://git.poster.place/verita84/arch/raw/branch/main/gentoo.sh
		scp verita84@nas.lan:~/configs/scripts/gentoo.sh .
	elif [[ $choice = 5 ]]; then
		fixSound
	else
		tweaks
	fi
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
	echo -e "\033[1;97m  ⚡ POSTER.PLACE GENTOO CYBERPUNK INSTALLER ⚡\033[0m"
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
