# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="PosterChan desktop — the shell PosterChanOS boots into"
HOMEPAGE="https://poster.place"
# The rolling release: one URL, re-tagged on every build. Fetched with the version in the local
# file name so portage's cache cannot serve yesterday's binary for today's version.
SRC_URI="https://github.com/loblawbob873-svg/posterchanai/releases/download/desktop-latest/PosterChan.AppImage -> ${P}.AppImage"
S="${WORKDIR}"

LICENSE="GPL-3"
SLOT="0"
KEYWORDS="~amd64"
RESTRICT="mirror strip"

# EXTRACTED AT BUILD TIME, NOT RUN AS AN APPIMAGE. An AppImage needs FUSE at runtime, which a minimal
# profile does not have; extracting once here needs it never and turns the shell into an ordinary
# directory of files.
RDEPEND="
	gui-wm/sway
	x11-base/xwayland
	media-video/pipewire
	media-video/wireplumber
"

src_unpack() {
	cp "${DISTDIR}/${P}.AppImage" "${WORKDIR}/app.AppImage" || die
	chmod +x "${WORKDIR}/app.AppImage" || die
	"${WORKDIR}/app.AppImage" --appimage-extract >/dev/null || die "could not extract the AppImage"
}

src_install() {
	insinto /opt/posterchan
	doins -r "${WORKDIR}"/squashfs-root/.
	# READABLE AND EXECUTABLE BY THE PEOPLE WHO RUN IT. `--appimage-extract` inherits the umask of
	# whatever ran it; installed at 0700 the one directory every session execs from is root-only.
	fperms -R a+rX /opt/posterchan
	fperms 0755 /opt/posterchan/AppRun
	fperms 0755 /opt/posterchan/posterchan-desktop
	# Electron refuses to start unless its sandbox helper is setuid root, and no archive can carry
	# that bit. The alternative is --no-sandbox, which turns the renderer sandbox off on a machine
	# strangers log into.
	fperms 4755 /opt/posterchan/chrome-sandbox

	# A WRAPPER, NOT A SYMLINK: AppRun finds the binary through $APPDIR, which the AppImage RUNTIME
	# sets — exactly the thing extracting removes. Symlinked, it resolves to "/posterchan-desktop:
	# No such file or directory", a path that was never real.
	newbin - posterchan <<-'WRAP'
		#!/bin/sh
		export APPDIR=/opt/posterchan
		export ELECTRON_OZONE_PLATFORM_HINT=auto
		exec "$APPDIR/AppRun" "$@"
	WRAP
}
