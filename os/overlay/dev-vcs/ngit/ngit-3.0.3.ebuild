# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="Nostr-native Git client and remote helper"
HOMEPAGE="https://github.com/DanConwayDev/ngit-cli"
RESTRICT="mirror"  # no Gentoo mirror carries this file; asking one is a wasted 404
SRC_URI="https://github.com/DanConwayDev/ngit-cli/releases/download/v${PV}/ngit-v${PV}-x86_64-unknown-linux-gnu.2.17.tar.gz -> ${P}.tar.gz"
S="${WORKDIR}"

LICENSE="MIT"
SLOT="0"
KEYWORDS="amd64"
RDEPEND="dev-vcs/git"

src_install() {
	dobin ngit git-remote-nostr
}
