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
IUSE="monero"

# Everything the session needs to be a desktop rather than a compositor with one window in it.
RDEPEND="
	app-misc/posterchan-desktop
	dev-vcs/ngit
	gui-wm/sway
	gui-apps/swayidle
	gui-apps/foot
	gui-apps/grim
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
	# The helpers. pc-key must obey the same limits as the on-screen controls; the repo's
	# tests/test_pc_key_limits.py is what keeps the two in step, and it runs before this is built.
	exeinto /usr/local/bin
	for helper in foot pc-provision-user pc-session-switch pc-shell-start pc-shell-restart pc-window-cycle pc-window-snap pc-window-close pc-key pc-idle pc-screenshot pc-monero-wallet-rpc update-posterchan; do
		doexe "${FILESDIR}/${helper}"
	done
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

	insinto /etc/sway
	# Sway reads /etc/sway/config. `doins sway.config` preserves the source filename and silently
	# creates /etc/sway/sway.config instead, leaving the compositor on the distro's old config.
	newins "${FILESDIR}/sway.config" config
	# Portage owns /etc/sway/config, so an `etc-update --automode -5` replaces a hand-edited one
	# with ours. That is the intended behaviour for a shipped session — and it is exactly what
	# silently reverted the config during development, so it is worth stating rather than
	# rediscovering.

	insinto /usr/share/plymouth/themes/posterchanos
	doins "${FILESDIR}"/plymouth/*

	insinto /etc/xdg/xdg-desktop-portal
	doins "${FILESDIR}/sway-portals.conf"
}

pkg_postinst() {
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
	# Identity accounts receive a private Sway config when they are provisioned. Keep the recovery
	# binding available to accounts created by an older image without replacing any personal Sway
	# customizations they may have made since. The live IPC binding is installed by the updater;
	# this copy is what makes it survive the next login.
	local cfg
	for cfg in "${EROOT%/}/home/posterchan/.config/sway/config" "${EROOT%/}"/home/pc-*/.config/sway/config; do
		[[ -f ${cfg} ]] || continue
		local cfg_backup="${cfg}.posterchan-pre-update"
		cp -p "${cfg}" "${cfg_backup}"
		# These are package-owned bindings inside an otherwise user-owned file. Remove every older
		# form first; merely checking for the keycode retained a stale command forever.
		sed -i -E '/Ctrl\+Mod1\+(BackSpace|22).*pc-shell-(start|restart)/d' "${cfg}"
		# Old migrations appended the two labels below on every upgrade even though their bindings
		# were replaced. Besides growing the config without bound, that made a private config drift
		# farther from the package-owned source on every release. They are package comments, not user
		# settings, so remove all copies before writing the single current recovery block below.
		sed -i -E \
			'/^# Screenshots work even while the desktop renderer is restarting\.$/d; /^# Restart only the PosterChan desktop shell; native applications remain open\.$/d' \
			"${cfg}"
		# Super+Return also fires the bare-Super release binding on some Sway/XKB paths, opening
		# Start over the terminal. Alt+Return is the shipped shortcut now; repair private configs
		# created by an older image so an update changes the key people actually use.
		sed -i -E 's#bindsym \$mod\+Return exec swaymsg -t send_tick pc:terminal#bindsym Mod1+Return exec swaymsg -t send_tick pc:terminal#' "${cfg}"
		sed -i 's#bindsym --release --no-repeat \$mod exec swaymsg -t send_tick pc:start#bindsym --release --no-repeat Super_L exec swaymsg -t send_tick pc:start#' "${cfg}"
		# Older PosterChan frames supplied their own HTML title bars, so those images deliberately
		# disabled Sway's floating decoration. Native applications are compositor-owned now; retaining
		# that copied setting leaves Firefox, Telegram and terminals with no title bar or resize border.
		# This is a package default migration, while any other per-user Sway changes remain untouched.
		sed -i -E 's/^default_floating_border[[:space:]]+none([[:space:]]*)$/default_floating_border normal 3\1/' "${cfg}"
		# Do not rely only on the catch-all rule for the two native applications users interact with
		# most. Old private configs may predate it or override it later; these last matching rules
		# guarantee a compositor frame and a floating container, which are the prerequisites for
		# dragging and pc-window-snap.
		for native_rule in \
			'for_window [app_id="firefox"] floating enable, border normal 3' \
			'for_window [class="(?i)^firefox$"] floating enable, border normal 3' \
			'for_window [app_id="org.telegram.desktop"] floating enable, border normal 3' \
			'for_window [class="(?i)^(TelegramDesktop|telegram-desktop)$"] floating enable, border normal 3'; do
			grep -qF "${native_rule}" "${cfg}" || echo "${native_rule}" >>"${cfg}"
		done
		# Private identity configs are copied from the image and do not inherit later changes to the
		# system config. Install the same compact PosterChan chrome here so native Firefox/Telegram
		# do not fall back to Sway's blue title bar after an update.
		for chrome_rule in \
			'font pango:Sans 11' \
			'titlebar_border_thickness 0' \
			'titlebar_padding 8 6' \
			'client.focused #241438 #241438 #f7f4ff #16d9e3 #16d9e3' \
			'client.focused_inactive #171222 #171222 #bcb3cb #4b3a65 #4b3a65' \
			'client.unfocused #100d18 #100d18 #8f879c #30263f #30263f' \
			'client.urgent #7a2145 #7a2145 #ffffff #ff4f8b #ff4f8b'; do
			grep -qF "${chrome_rule}" "${cfg}" || echo "${chrome_rule}" >>"${cfg}"
		done
		# Super+Arrow is the familiar snap gesture. Older configs used it only to move keyboard focus
		# between outputs, leaving native Firefox/Telegram/Steam with no snapping at all.
		sed -i -E '/^bindsym[[:space:]]+\$mod\+(Left|Right|Up|Down)[[:space:]]+focus output/d' "${cfg}"
		# Canonicalise, do not merely append. Historical package configs used extra whitespace, so an
		# exact grep missed Left/Up and appended the same key a second time. Sway accepts that but emits
		# an "Overwriting binding" config error on every reload.
		sed -i -E '/^bindsym[[:space:]]+\$mod\+(Left|Right|Up)[[:space:]]+exec[[:space:]]+\/usr\/local\/bin\/pc-window-snap[[:space:]]+(left|right|max)[[:space:]]*$/d' "${cfg}"
		for snap in \
			'bindsym $mod+Left exec /usr/local/bin/pc-window-snap left' \
			'bindsym $mod+Right exec /usr/local/bin/pc-window-snap right' \
			'bindsym $mod+Up exec /usr/local/bin/pc-window-snap max'; do
			echo "${snap}" >>"${cfg}"
		done
		# Never move a per-output PosterChan shell container itself. Older direct bindings bypassed
		# the renderer handoff and left the source display black. Native apps still move directly;
		# a focused shell routes the selected in-app window through its state-preserving handoff.
		sed -i -E '/^bindsym[[:space:]]+\$mod\+Shift\+(Left|Right|Up|Down)[[:space:]]+(move container|exec \/usr\/local\/bin\/pc-window-snap move-)/d' "${cfg}"
		for move_binding in \
			'bindsym $mod+Shift+Left exec /usr/local/bin/pc-window-snap move-left' \
			'bindsym $mod+Shift+Right exec /usr/local/bin/pc-window-snap move-right' \
			'bindsym $mod+Shift+Up exec /usr/local/bin/pc-window-snap move-up' \
			'bindsym $mod+Shift+Down exec /usr/local/bin/pc-window-snap move-down'; do
			echo "${move_binding}" >>"${cfg}"
		done
		# Options such as --no-repeat sit between `bindsym` and the key. The old expression did not
		# allow that, so every package update appended another identical PrintScreen binding and Sway
		# reported the private config as erroneous. Delete every historical form before adding one.
		sed -i -E '/bindsym .*?(Print|Ctrl\+Shift\+s|Shift\+Print).*pc:(shot|screenshot)/d' "${cfg}"
		sed -i -E '/bindsym .*?(Print|Ctrl\+Shift\+s|Shift\+Print).*pc-screenshot/d' "${cfg}"
		if ! grep -q 'Super_L exec swaymsg -t send_tick pc:start' "${cfg}"; then
			echo 'bindsym --release --no-repeat Super_L exec swaymsg -t send_tick pc:start' >>"${cfg}"
		fi
		# Identity configs are copies, not includes of /etc/sway/config. Accounts made before Display
		# Settings therefore never read the file the UI successfully saved, and every reboot reverted
		# the monitor layout. Add the two new compositor hooks once without replacing custom config.
		grep -qF 'include ~/.config/sway/outputs.conf' "${cfg}" || \
			echo 'include ~/.config/sway/outputs.conf' >>"${cfg}"
		grep -qE '^floating_modifier[[:space:]]+\$mod[[:space:]]+normal' "${cfg}" || \
			echo 'floating_modifier $mod normal' >>"${cfg}"
		grep -qF 'Mod1+Tab exec /usr/local/bin/pc-window-cycle next' "${cfg}" || \
			echo 'bindsym --no-repeat Mod1+Tab exec /usr/local/bin/pc-window-cycle next' >>"${cfg}"
		grep -qF 'Mod1+Shift+Tab exec /usr/local/bin/pc-window-cycle previous' "${cfg}" || \
			echo 'bindsym --no-repeat Mod1+Shift+Tab exec /usr/local/bin/pc-window-cycle previous' >>"${cfg}"
		# CLOSE CHORDS: TAKE THE BARE `kill` BACK OUT. An existing config carries
		# `bindsym $mod+q kill` / `bindsym Mod1+F4 kill`, and this block used to ADD the
		# second one to any config that lacked it. sway's `kill` closes the focused
		# CONTAINER, which is the single shell surface hosting every PosterChan window
		# whenever the desktop has focus -- so Alt+F4 destroyed the whole desktop rather
		# than the window it was aimed at, and an upgrade installed that on machines that
		# had escaped it. Delete both spellings, then bind the helper that asks what is
		# focused first. Matching only a trailing bare `kill` leaves a hand-written
		# binding that runs anything else alone.
		sed -i -E '/^bindsym[[:space:]]+(\$mod\+q|Mod1\+F4)[[:space:]]+kill[[:space:]]*$/d' "${cfg}"
		sed -i -E '/^bindsym[[:space:]]+(\$mod\+q|Mod1\+F4)[[:space:]]+exec[[:space:]]+\/usr\/local\/bin\/pc-window-close[[:space:]]*$/d' "${cfg}"
		for line in \
			'bindsym $mod+q exec /usr/local/bin/pc-window-close' \
			'bindsym Mod1+F4 exec /usr/local/bin/pc-window-close'; do
			echo "${line}" >>"${cfg}"
		done
		sed -i -E '/bindsym .*button1 exec \/usr\/local\/bin\/pc-window-snap edge/d' "${cfg}"
		echo 'bindsym --border --release button1 exec /usr/local/bin/pc-window-snap edge' >>"${cfg}"
		cat >>"${cfg}" <<-'SWAY_RECOVERY'

		# Screenshots work even while the desktop renderer is restarting.
		bindsym --no-repeat Print exec /usr/local/bin/pc-screenshot region
		bindsym --no-repeat Ctrl+Shift+s exec /usr/local/bin/pc-screenshot region
		bindsym --no-repeat Shift+Print exec /usr/local/bin/pc-screenshot screen

		# Restart only the PosterChan desktop shell; native applications remain open.
		bindcode --no-repeat Ctrl+Mod1+22 exec /usr/local/bin/pc-shell-restart
		SWAY_RECOVERY
		# `sway -C` alone exits zero for duplicate keys. Debug output is part of the gate because Sway
		# reports those as "Overwriting binding" and shows a config-error banner at reload.
		local sway_runtime sway_check
		sway_runtime="$(mktemp -d)"
		chmod 0700 "${sway_runtime}"
		sway_check="${sway_runtime}/check.log"
		if ! XDG_RUNTIME_DIR="${sway_runtime}" WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 \
			sway -C -d -c "${cfg}" >"${sway_check}" 2>&1 \
			|| grep -q 'Overwriting binding' "${sway_check}"; then
			cp -p "${cfg_backup}" "${cfg}"
			rm -rf "${sway_runtime}" "${cfg_backup}"
			ewarn "refused an invalid migrated Sway config; preserved ${cfg}"
			continue
		fi
		rm -rf "${sway_runtime}" "${cfg_backup}"
		# The system config includes this per-account file. It must exist before Sway parses the
		# config; saving a display arrangement later fills it atomically.
		local outputs="${cfg%/config}/outputs.conf"
		if [[ ! -e ${outputs} ]]; then
			install -m 0600 -o "$(stat -c %u "${cfg}")" -g "$(stat -c %g "${cfg}")" \
				/dev/null "${outputs}"
		fi
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
