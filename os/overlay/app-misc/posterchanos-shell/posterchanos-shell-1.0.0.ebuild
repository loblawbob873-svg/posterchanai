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

# Everything the session needs to be a desktop rather than a compositor with one window in it.
RDEPEND="
	app-misc/posterchan-desktop
	gui-wm/sway
	gui-apps/swayidle
	gui-apps/foot
	sys-apps/xdg-desktop-portal
	gui-libs/xdg-desktop-portal-wlr
	sys-boot/plymouth
"

src_install() {
	# The helpers. pc-key must obey the same limits as the on-screen controls; the repo's
	# tests/test_pc_key_limits.py is what keeps the two in step, and it runs before this is built.
	exeinto /usr/local/bin
	for helper in pc-provision-user pc-session-switch pc-shell-start pc-shell-restart pc-key pc-idle update-posterchan; do
		doexe "${FILESDIR}/${helper}"
	done
	# The installed recovery/LiveUSB tool is package-owned too. publish_overlay.sh injects the
	# canonical os/gentoo.sh into FILESDIR, so an ordinary update cannot leave an older installer.
	dobin "${FILESDIR}/gentoo.sh"

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
	# Identity accounts receive a private Sway config when they are provisioned. Keep the recovery
	# binding available to accounts created by an older image without replacing any personal Sway
	# customizations they may have made since. The live IPC binding is installed by the updater;
	# this copy is what makes it survive the next login.
	local cfg
	for cfg in "${EROOT%/}/home/posterchan/.config/sway/config" "${EROOT%/}"/home/pc-*/.config/sway/config; do
		[[ -f ${cfg} ]] || continue
		# These are package-owned bindings inside an otherwise user-owned file. Remove every older
		# form first; merely checking for the keycode retained a stale command forever.
		sed -i -E '/Ctrl\+Mod1\+(BackSpace|22).*pc-shell-(start|restart)/d' "${cfg}"
		sed -i 's#bindsym --release --no-repeat \$mod exec swaymsg -t send_tick pc:start#bindsym --release --no-repeat Super_L exec swaymsg -t send_tick pc:start#' "${cfg}"
		if ! grep -q 'Super_L exec swaymsg -t send_tick pc:start' "${cfg}"; then
			echo 'bindsym --release --no-repeat Super_L exec swaymsg -t send_tick pc:start' >>"${cfg}"
		fi
		cat >>"${cfg}" <<-'SWAY_RECOVERY'

		# Restart only the PosterChan desktop shell; native applications remain open.
		bindcode --no-repeat Ctrl+Mod1+22 exec /usr/local/bin/pc-shell-restart
		SWAY_RECOVERY
	done

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
	fi
	elog "PosterChanOS session installed."
	elog "Autologin is configured by the installer, not by this package."
}
