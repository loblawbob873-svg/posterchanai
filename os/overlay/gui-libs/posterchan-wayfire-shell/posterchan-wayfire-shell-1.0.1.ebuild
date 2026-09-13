# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3
EAPI=8
inherit multilib toolchain-funcs
DESCRIPTION="Per-surface movement protection for the PosterChan Wayfire desktop"
HOMEPAGE="https://poster.place"
S="${WORKDIR}"
LICENSE="GPL-3"
SLOT="0"
KEYWORDS="amd64"
DEPEND="gui-wm/wayfire:="
RDEPEND="${DEPEND}"
BDEPEND="virtual/pkgconfig"
src_compile() {
    "$(tc-getCXX)" ${CXXFLAGS} -std=c++17 -fPIC -shared \
        $($(tc-getPKG_CONFIG) --cflags wayfire) \
        "${FILESDIR}/posterchan-shell.cpp" -o libposterchan-shell.so \
        ${LDFLAGS} $($(tc-getPKG_CONFIG) --libs wayfire) || die
}
src_install() {
    insinto /usr/$(get_libdir)/wayfire
    doins libposterchan-shell.so
    insinto /usr/share/wayfire/metadata
    doins "${FILESDIR}/posterchan-shell.xml"
}
