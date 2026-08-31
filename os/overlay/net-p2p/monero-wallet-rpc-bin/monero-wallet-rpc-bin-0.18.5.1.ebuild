# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="Official prebuilt Monero wallet RPC for PosterChanOS"
HOMEPAGE="https://www.getmonero.org/"
SRC_URI="https://downloads.getmonero.org/cli/monero-linux-x64-v${PV}.tar.bz2 -> ${P}.tar.bz2"
S="${WORKDIR}/monero-x86_64-linux-gnu-v${PV}"

LICENSE="BSD"
SLOT="0"
KEYWORDS="amd64"
RESTRICT="mirror strip"

# The archive checksum was taken from getmonero.org's clearsigned hashes.txt. Its signer is
# binaryFate, fingerprint 81AC 591F E9C4 B65C 5806 AFC3 F0AF 4D46 2A0B DF92. Keep the immutable
# evidence beside this ebuild; tests tie that signed SHA-256 to this version and Portage Manifest.

src_install() {
	dobin monero-wallet-rpc
	dodoc README.md ANONYMITY_NETWORKS.md
	newdoc LICENSE LICENSE.monero
}
