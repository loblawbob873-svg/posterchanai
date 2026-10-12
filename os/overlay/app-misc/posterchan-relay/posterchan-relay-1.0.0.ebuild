# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="PosterChanOS local relay: the desktop's event cache, one PosterChanDB store per account"
HOMEPAGE="https://poster.place"
S="${WORKDIR}"

LICENSE="GPL-3"
SLOT="0"
KEYWORDS="amd64"

# THE CODE IS THE SERVER'S, THE INTERPRETER IS THE OS'S.
#
# The relay is app/services/nostr_relay/osmode.py inside app-misc/posterchan-server (/opt/posterchan-server),
# which installs the code and nothing else. The server's own venv is built only when somebody enables the
# server, so this package does NOT use it: --os-mode imports only numpy, websockets and httpx (asserted by
# tests/test_os_relay_import_closure.py against a bare venv), all ordinary Gentoo packages. Signatures are
# checked through libsecp256k1 by ctypes (app/services/nostr/bip340.py) -- the C library, not a Python
# binding with no wheel for the OS's Python; a library without the post-0.2 schnorrsig ABI is refused and
# the relay falls back to the (slow) reference verifier rather than mis-calling it.
RDEPEND="
	app-misc/posterchan-server
	>=dev-lang/python-3.12
	dev-python/numpy
	dev-python/websockets
	dev-python/httpx
	dev-libs/libsecp256k1
	sys-apps/systemd
	sys-apps/coreutils
	sys-fs/e2fsprogs
"

src_install() {
	# The launcher (one source: os/bin/pc-local-relay, injected by publish_overlay.sh) and the USER unit.
	exeinto /usr/local/bin
	doexe "${FILESDIR}/pc-local-relay"
	insinto /usr/lib/systemd/user
	doins "${FILESDIR}/posterchan-relay.service"
}

pkg_postinst() {
	# GLOBALLY, for every account: accounts are created when somebody first signs in with a key, long after
	# this package was installed, and a --user enable cannot reach an account that does not exist yet.
	systemctl --global enable posterchan-relay.service >/dev/null 2>&1 \
		|| ewarn "could not enable posterchan-relay.service for every account"
	elog "The desktop's event cache starts at the next sign-in (or: systemctl --user start posterchan-relay)."
	elog "Until it is listening the desktop keeps using its IndexedDB cache, exactly as before."
}

pkg_prerm() {
	[[ -z ${REPLACED_BY_VERSION} ]] || return 0
	systemctl --global disable posterchan-relay.service >/dev/null 2>&1 || true
}
