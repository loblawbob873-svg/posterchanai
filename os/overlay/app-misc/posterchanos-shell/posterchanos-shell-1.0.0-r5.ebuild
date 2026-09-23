# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="PosterChanOS session: compositor config, helpers, boot splash"
HOMEPAGE="https://poster.place"
S="${WORKDIR}"

LICENSE="GPL-3"
SLOT="0"
# STABLE, not ~amd64. The convention of starting a new ebuild in testing exists so a
# distribution can stage third-party software — but this overlay IS the release channel
# for these packages, and publishing one is what shipping it means. Keyworded ~amd64 they
# are masked on every stable install, which is every PosterChanOS machine, and the error
# a person sees is "all ebuilds have been masked" about software their own OS ships.
KEYWORDS="amd64"
IUSE="monero gamescope"

# Everything the session needs to be a desktop rather than a compositor with one window in it.
RDEPEND="
	>=dev-lang/python-3.10
	>=app-misc/posterchan-desktop-1.0.1660
	app-misc/posterchan-server
	dev-vcs/ngit
	>=gui-wm/wayfire-0.10.1-r2
	>=gui-libs/posterchan-wayfire-shell-1.0.1-r6
	gui-libs/wayfire-plugins-extra
	gamescope? ( gui-wm/gamescope )
	gui-apps/swayidle
	gui-apps/foot
	gui-apps/grim
	gui-apps/wlr-randr
	gui-apps/slurp
	gui-apps/wl-clipboard
	x11-misc/ydotool
	app-misc/ddcutil
	www-client/firefox-bin
	net-wireless/bluez
	sys-apps/xdg-desktop-portal
	gui-libs/xdg-desktop-portal-wlr
	sys-boot/plymouth
	monero? ( net-p2p/monero-wallet-rpc-bin )
"

src_install() {
	insinto /usr/local/libexec/posterchanos/nostr
	doins "${FILESDIR}/bip340.py" "${FILESDIR}/bech32.py"
	# pc-usb-grant's scanner: the VM host's own app/services/vmhost/usb.py, injected by publish_overlay.sh.
	insinto /usr/local/lib/posterchan
	doins "${FILESDIR}/pc_usb_scan.py"
	# The helpers. pc-key must obey the same limits as the on-screen controls; the repo's
	# tests/test_pc_key_limits.py is what keeps the two in step, and it runs before this is built.
	exeinto /usr/local/bin
	for helper in foot pc-super pc-provision-user pc-session-switch pc-session-auth pc-compositor-session pc-wayfire-action pc-wayfire-health pc-shell-start-wayfire pc-shell-restart pc-window-cycle pc-window-snap pc-window-close pc-key pc-idle pc-pointer-confine pc-screenshot pc-monero-wallet-rpc pc-open pc-kernel-guard pc-usb-grant update-posterchan; do
		doexe "${FILESDIR}/${helper}"
	done
	# `pc-open` asks the running desktop to open a file in Office, Code, Preview or Files. The
	# aliases are the same program: it reads its own name, so `pc-office x` is `pc-open --office x`.
	local alias
	for alias in pc-office pc-code pc-preview pc-files; do
		dosym pc-open "/usr/local/bin/${alias}"
	done
	# ...and `xdg-open report.docx` reaches it too. Nothing on this machine claimed an office
	# document, so xdg-open had no answer at all; the defaults cover ONLY those types (see the file).
	insinto /usr/share/applications
	doins "${FILESDIR}/posterchan-open.desktop"
	insinto /etc/xdg
	newins "${FILESDIR}/posterchanos-mimeapps.list" mimeapps.list
	insinto /usr/lib/systemd/user
	doins "${FILESDIR}/posterchan-monero-wallet-rpc.service"
	# The installed recovery/LiveUSB tool is package-owned too. publish_overlay.sh injects the
	# canonical os/gentoo.sh into FILESDIR, so an ordinary update cannot leave an older installer.
	dobin "${FILESDIR}/gentoo.sh"
	# Automatic clean-image publication is part of the installed LiveCD command, not repository-only
	# tooling. Keep the publisher outside PATH: gentoo.sh invokes it by this exact package-owned path.
	exeinto /usr/local/libexec/posterchanos
	doexe "${FILESDIR}/publish_iso.sh"

	# The greeter may create an identity account, and an identity session may switch to another
	# identity. Keep both grants package-owned: gentoo.sh writes the same rules during an install,
	# but an already-installed machine must gain them through update-posterchan too.
	insinto /etc/sudoers.d
	newins "${FILESDIR}/posterchan-provision.sudoers" posterchan-provision
	newins "${FILESDIR}/posterchan-session-switch.sudoers" posterchan-session-switch
	fperms 0440 /etc/sudoers.d/posterchan-provision
	fperms 0440 /etc/sudoers.d/posterchan-session-switch
	# ONE USB device for the caller's own VM; the helper checks every argument itself.
	newins "${FILESDIR}/posterchan-usb-grant.sudoers" posterchan-usb-grant
	fperms 0440 /etc/sudoers.d/posterchan-usb-grant

	# The session config. Portage owns /etc/wayfire.ini, so an `etc-update --automode -5` replaces a
	# hand-edited one with ours. That is the intended behaviour for a shipped session — and it is
	# exactly what silently reverted the config during development, so it is worth stating rather
	# than rediscovering.
	insinto /etc
	doins "${FILESDIR}/wayfire.ini"

	insinto /usr/share/plymouth/themes/posterchanos
	doins "${FILESDIR}"/plymouth/*

	# NAMED FOR NO DESKTOP, because it applies to the only one there is. xdg-desktop-portal matches
	# `<XDG_CURRENT_DESKTOP>-portals.conf` first, so the old `sway-portals.conf` selected nothing on a
	# session announcing `wayfire`: the ScreenCast backend went unset and OBS listed nothing to
	# capture. `portals.conf` is the unqualified fallback the portal always reads.
	insinto /etc/xdg/xdg-desktop-portal
	newins "${FILESDIR}/portals.conf" portals.conf

	# FIREFOX CANNOT BE GIVEN A COMPOSITOR FRAME, SO IT IS TOLD TO DRAW ONE.
	#
	# `preferred_decoration_mode = server` only reaches clients that speak zxdg_decoration_manager_v1.
	# Qt does (OBS gets a Wayfire title bar); GTK does not, and MEASURED on this machine there is not
	# one occurrence of that interface name anywhere in Firefox's libxul. So no compositor setting,
	# per-app rule or window rule can put a frame on Firefox -- reported as "firefox has no window
	# decoration" beside "OBS has totally different window decorations than the rest of the OS", which
	# is the same fact from both ends. With `browser.tabs.inTitlebar` at its Linux default Firefox
	# draws its tab strip where a title bar goes, which is why it reads as having none.
	#
	# `Status: default` rather than locked: this is the shipped default and the user may still change
	# it in about:config, exactly like every other preference on their own machine.
	#
	# THE CYBERPUNK THEME RIDES THE SAME FILE, AND ONLY WHEN ITS SIGNED COPY IS HERE. Release Firefox
	# discards an unsigned theme without a word (measured: no entry in extensions.json at all), so
	# publish_overlay.sh injects the addons.mozilla.org-signed .xpi into FILESDIR or nothing. The
	# `Extensions.Install` policy runs once per change of its list -- the version is in the file
	# name, so an upgrade re-runs it -- and never again after the person picks another theme
	# (measured). `extensions.activeThemeID` as a DEFAULT makes it the theme of every new profile.
	# Naming a file that is not there would spend that one run on an error, which is why the theme
	# half is only written when the file exists.
	local theme=( "${FILESDIR}"/posterchan-cyberpunk-*.xpi )
	if [[ -f ${theme[0]} ]]; then
		insinto /usr/share/posterchanos/firefox
		doins "${theme[0]}"
		sed -e "s|@THEME_XPI@|/usr/share/posterchanos/firefox/${theme[0]##*/}|" \
			"${FILESDIR}/firefox-policies-theme.json" > "${T}/policies.json" || die
		insinto /etc/firefox/policies
		newins "${T}/policies.json" policies.json
	else
		insinto /etc/firefox/policies
		newins "${FILESDIR}/firefox-policies.json" policies.json
	fi
}

pkg_postinst() {
	# The MimeType= cache, so "Open with" lists PosterChan Office. The DEFAULTS above do not need it
	# (mimeapps.list names the desktop file directly), which is why a missing tool is not an error.
	if [[ -z ${ROOT} ]] && command -v update-desktop-database >/dev/null 2>&1; then
		update-desktop-database -q "${EROOT%/}/usr/share/applications" || true
	fi
	# Remote Desktop control uses ydotool's per-user 0600 socket. Enable it globally so each signed-in
	# identity gets its own daemon/socket; the desktop also starts it lazily for already-open sessions.
	systemctl --global enable ydotool.service >/dev/null 2>&1 || true
	# One repository endpoint on every PosterChanOS machine. The NAS maintains the local mirror and
	# gentoo.poster.place serves it over HTTPS; clients consume Gentoo's signed webrsync snapshot.
	# Keep this in pkg_postinst as well as gentoo.sh so a normal OS update repairs existing installs.
	local repodir="${EROOT%/}/etc/portage/repos.conf"
	local binrepo="${EROOT%/}/etc/portage/binrepos.conf/gentoobinhost.conf"
	local makeconf="${EROOT%/}/etc/portage/make.conf"
	install -d -m 0755 "${repodir}" "${binrepo%/*}"
	cat >"${repodir}/gentoo-mirror.conf" <<-'REPO'
		[gentoo]
		location = /var/db/repos/gentoo
		sync-type = webrsync
		sync-uri = https://gentoo.poster.place
		sync-webrsync-verify-signature = true
	REPO
	cat >"${binrepo}" <<-'BINREPO'
		[binhost]
		priority = 9999
		sync-type = webrsync
		sync-uri = https://gentoo.poster.place/releases/amd64/binpackages/23.0/x86-64/
	BINREPO
	sed -i '/^[[:space:]]*GENTOO_MIRRORS=/d' "${makeconf}"
	echo 'GENTOO_MIRRORS="https://gentoo.poster.place"' >>"${makeconf}"
	# THE PER-ACCOUNT SWAY CONFIGS ARE RETIRED, NOT MIGRATED.
	#
	# ~150 lines here used to reach into every identity's private `~/.config/sway/config` and rewrite
	# package-owned bindings inside it on each upgrade -- because provisioning gave each account its
	# own copy of the compositor config rather than a reference to one. Wayfire is the only session
	# now, its config is a single package-owned `/etc/wayfire.ini`, and pc-session-switch no longer
	# writes a per-account compositor config at all, so there is nothing left to keep in step.
	#
	# What remains is cleanup: leaving `~/.config/sway/` behind means a directory that looks like
	# live configuration, and an `outputs.conf` that looks like somebody's saved monitor layout while
	# nothing reads it. Moved rather than deleted -- it is the user's file, and an upgrade should not
	# be the thing that destroys one.
	local cfg
	for cfg in "${EROOT%/}/home/posterchan/.config/sway" "${EROOT%/}"/home/pc-*/.config/sway; do
		[[ -d ${cfg} ]] || continue
		mv -T "${cfg}" "${cfg}.retired-sway" 2>/dev/null || true
	done
	# Older renderer-driven bindings could auto-repeat and leave several slurp selection overlays
	# dimming native applications. The new helper is locked and non-repeating; clear only those stale
	# selectors once during the upgrade so the desktop is immediately usable again.
	pkill -x slurp >/dev/null 2>&1 || true

	# THE THEME LIVES INSIDE THE INITRAMFS. Setting it without rebuilding leaves the previous splash
	# on screen and gives no hint as to why.
	if [[ -z ${ROOT} ]]; then
		plymouth-set-default-theme -R posterchanos || \
			ewarn "could not set the boot splash — run: plymouth-set-default-theme -R posterchanos"
		# Sound is enabled GLOBALLY, not per user: accounts here are created when somebody signs in
		# with a key, long after this package was installed, and a --user enable cannot reach an
		# account that does not exist yet.
		systemctl --global enable pipewire.socket pipewire-pulse.socket wireplumber.service \
			>/dev/null 2>&1 || true
		# gentoo.sh has always enabled this on a fresh install, but an existing PosterChanOS
		# machine learns about the Bluetooth panel through update-posterchan. Make that upgrade
		# complete as well: install BlueZ through RDEPEND and bring its system daemon up now and
		# on every later boot. Without this, bluetoothctl waits for a daemon that is not running
		# and the on-screen Bluetooth button appears to do nothing.
		systemctl enable --now bluetooth.service >/dev/null 2>&1 || \
			ewarn "could not start Bluetooth — check: systemctl status bluetooth.service"
	fi
	elog "PosterChanOS session installed."
	elog "Autologin is configured by the installer, not by this package."
}
