# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="pvim: PosterChanOS's text editor -- vim's keys, neon on black"
HOMEPAGE="https://poster.place"
S="${WORKDIR}"

LICENSE="GPL-3"
SLOT="0"
KEYWORDS="amd64"

# ONE FILE, THE OS'S OWN PYTHON. The editor is os/bin/pvim in the PosterChan repo (injected here by
# publish_overlay.sh, which also gives this package a timestamp version so a changed editor installs).
# It needs nothing but the standard library's curses -- python is already on every Gentoo system, which
# is what lets virtual/editor name pvim alone and a fresh install carry no nano, vim or neovim.
RDEPEND="
	>=dev-lang/python-3.11[ncurses]
	sys-libs/ncurses
"

src_install() {
	exeinto /usr/bin
	doexe "${FILESDIR}/pvim"
	# EDITOR/VISUAL for every login, root included (env.d, written into /etc/profile.env by env-update).
	doenvd "${FILESDIR}/99pvim"
	insinto /etc/profile.d
	newins "${FILESDIR}/pvim-aliases.sh" pvim-aliases.sh
	insinto /etc/bash/bashrc.d
	newins "${FILESDIR}/pvim-aliases.sh" 50-pvim-aliases.sh
}

pkg_postinst() {
	elog "pvim is the system editor (EDITOR/VISUAL). :help inside it lists every key."
	elog "Run 'env-update && source /etc/profile' in an already-open shell to pick it up now."
}
