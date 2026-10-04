# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3
EAPI=8
inherit multilib toolchain-funcs
DESCRIPTION="Per-surface movement protection, fullscreen pointer confinement and the accent window border for the PosterChan Wayfire desktop"
HOMEPAGE="https://poster.place"
S="${WORKDIR}"
LICENSE="GPL-3"
SLOT="0"
KEYWORDS="amd64"
DEPEND="gui-wm/wayfire:="
RDEPEND="${DEPEND}"
# wayland-scanner (dev-libs/wayland) and the protocol XML (dev-libs/wayland-protocols) are build
# inputs, not runtime ones -- see the header note in src_compile.
BDEPEND="virtual/pkgconfig dev-libs/wayland dev-libs/wayland-protocols"
src_compile() {
    # WLROOTS DOES NOT INSTALL THE GENERATED PROTOCOL HEADERS, so an out-of-tree plugin cannot
    # include <wlr/types/wlr_pointer_constraints_v1.h> -- it `#include`s
    # "pointer-constraints-unstable-v1-protocol.h", which exists only inside a wlroots build tree.
    # That is why wayfire's own nonstd/wlroots-full.hpp wraps the include in an __has_include guard,
    # and why without this step the pointer-constraint check below compiles away to
    # "wlr_pointer_constraints_v1_constraint_for_surface was not declared in this scope".
    # Generating it from the installed XML is the supported way to get it; the build FAILS rather
    # than dropping the guard, because the guard is what keeps this plugin's clamp away from a
    # game's own mouse-look.
    wayland-scanner server-header \
        "$($(tc-getPKG_CONFIG) --variable=pkgdatadir wayland-protocols)/unstable/pointer-constraints/pointer-constraints-unstable-v1.xml" \
        pointer-constraints-unstable-v1-protocol.h || die
    # The same trap for xdg-shell: wlroots-full.hpp includes wlr_xdg_shell.h only when the generated
    # header is on the path, and the window border reads a client-decorated window's geometry from
    # it. The plugin #errors without it rather than quietly framing the wrong rectangle.
    wayland-scanner server-header \
        "$($(tc-getPKG_CONFIG) --variable=pkgdatadir wayland-protocols)/stable/xdg-shell/xdg-shell.xml" \
        xdg-shell-protocol.h || die
    "$(tc-getCXX)" ${CXXFLAGS} -std=c++17 -fPIC -shared -I"${S}" \
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
