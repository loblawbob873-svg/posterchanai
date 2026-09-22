# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="PosterChan server (Nostr relay + web client), bundled with PosterChanOS and OFF until enabled"
HOMEPAGE="https://poster.place"
# THE SERVER'S CODE, PINNED TO ONE COMMIT OF THE PUBLIC MIRROR. A commit archive never changes, so the
# Manifest's digest stays true — the same reason the desktop ships a per-version tarball rather than
# a rolling URL. scripts/pin_server_overlay.sh re-pins this line (and renames the ebuild to a newer
# version) whenever an overlay publish carries server changes; this committed value is the fallback.
PC_COMMIT="adaa1eadeb179c2b50688fbcc4423c574606d92e"
SRC_URI="https://github.com/loblawbob873-svg/posterchanai/archive/${PC_COMMIT}.tar.gz -> ${P}.tar.gz"
S="${WORKDIR}/posterchanai-${PC_COMMIT}"

LICENSE="GPL-3"
SLOT="0"
KEYWORDS="amd64"
RESTRICT="mirror strip"

# WHAT IS INSTALLED AND WHAT IS NOT, and why the line is there.
#
# Installed: the code, the database server, the interpreter the installer accepts, and the toolchain
# its AI path compiles llama.cpp with. All small (measured binpkgs: postgresql-18 11 MB, python-3.13
# 9 MB). NOT installed: the Python environment. requirements-nostr.txt measured 126 MB to download and
# 503 MB installed, which would more than double the cost of a feature that is off on almost every
# machine — so it is built by ./install.sh --nostr-only the first time somebody presses Enable.
#
# PYTHON 3.13, NOT THE SYSTEM'S. PosterChanOS runs 3.14, and install.sh refuses it (coincurve and
# libtorrent have no 3.14 wheels; building them from source fails). 3.13 is a separate slot and changes
# nothing else on the machine.
RDEPEND="
	acct-user/posterchan-server
	dev-db/postgresql[server]
	dev-lang/python:3.13
	dev-vcs/git
	dev-build/cmake
	app-admin/sudo
	sys-apps/systemd
"

src_install() {
	# The server, without what only a developer or another platform needs.
	rm -rf tests mobile desktop docs extension os .github || die
	dodir /opt/posterchan-server
	cp -a . "${ED}/opt/posterchan-server/" || die "could not install the server"
	# ITS OWN TREE BELONGS TO ITS OWN ACCOUNT. The server writes beside its code (data/, the venv the
	# installer builds, runtime state), exactly as it does in a checkout on any other node.
	fowners -R posterchan-server:posterchan-server /opt/posterchan-server

	# The one privileged door, and the rule that lets an administrator's desktop through it with a
	# fixed list of verbs. From FILESDIR (a copy of os/bin/pc-server kept identical by
	# tests/test_pc_server_helper.py), so the helper is the overlay's and not whatever the pinned
	# commit happened to carry.
	exeinto /usr/local/bin
	doexe "${FILESDIR}/pc-server"
	insinto /etc/sudoers.d
	newins "${FILESDIR}/posterchan-server.sudoers" posterchan-server
	fperms 0440 /etc/sudoers.d/posterchan-server
	keepdir /var/lib/posterchan-server
	fowners posterchan-server:posterchan-server /var/lib/posterchan-server
}

pkg_postinst() {
	# New code reaches a server that is RUNNING. One that is off stays off — an update must never be
	# the thing that switches it on.
	if [[ -z ${ROOT} ]] && systemctl is-active --quiet posterchanai.service 2>/dev/null; then
		systemctl try-restart posterchanai.service || ewarn "could not restart posterchanai.service"
	fi
	elog "The PosterChan server is installed but OFF. Turn it on in System Settings → PosterChan Server."
}
