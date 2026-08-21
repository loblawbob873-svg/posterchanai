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
	gui-apps/foot
	sys-apps/xdg-desktop-portal
	gui-libs/xdg-desktop-portal-wlr
	sys-boot/plymouth
"

src_install() {
	# The helpers. pc-key must obey the same limits as the on-screen controls; the repo's
	# tests/test_pc_key_limits.py is what keeps the two in step, and it runs before this is built.
	for helper in pc-provision-user pc-shell-start pc-key update-posterchan; do
		dobin "${FILESDIR}/${helper}"
	done

	insinto /etc/sway
	doins "${FILESDIR}/sway.config"
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
