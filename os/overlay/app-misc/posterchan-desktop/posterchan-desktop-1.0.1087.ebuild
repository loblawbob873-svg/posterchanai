# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="PosterChan desktop — the shell PosterChanOS boots into"
HOMEPAGE="https://poster.place"
# A VERSIONED TARBALL, AND THAT IS WHAT MAKES A MANIFEST POSSIBLE.
#
# This used to fetch PosterChan.AppImage from the rolling `desktop-latest` tag -- one URL,
# re-uploaded on every build. A Manifest pins a digest, so an ebuild naming a MUTABLE url verifies
# today and fails for everybody on the next desktop build, with no change to the overlay. The
# failure looks like a corrupt download and is actually a design error. Without a Manifest at all,
# portage refuses outright: "VERIFY FAILED! Reason: Insufficient data for checksum verification".
#
# The release now also carries PosterChan-<version>-linux-x64.tar.zst: the same files
# electron-builder produces on its way to building the image, under a name that cannot change. No
# FUSE, no AppImage runtime, nothing that verifies itself -- and a digest that stays true.
#
# AND IT IS FETCHED FROM A PER-VERSION TAG, NOT FROM `desktop-latest`. That was the other half, and
# without it the paragraph above only moved the problem: the FILENAME could not change, but the
# rolling release is DELETED AND RE-CREATED on every desktop build (see the "Retire the previous
# rolling release" step in .github/workflows/desktop.yml), which takes every previously-versioned
# tarball with it. So a Manifest that verified the day it was written 404s on the next desktop
# build -- not "fails checksum", cannot be downloaded at all -- and the overlay had gone stale
# three times that way (1.0.818, 1.0.825, and this one) before anybody looked. `desktop-v<version>`
# is written once and never touched again.
SRC_URI="https://github.com/loblawbob873-svg/posterchanai/releases/download/desktop-v${PV}/PosterChan-${PV}-linux-x64.tar.zst -> ${P}.tar.zst"
S="${WORKDIR}"

LICENSE="GPL-3"
SLOT="0"
# STABLE, not ~amd64. The convention of starting a new ebuild in testing exists so a
# distribution can stage third-party software — but this overlay IS the release channel
# for these packages, and publishing one is what shipping it means. Keyworded ~amd64 they
# are masked on every stable install, which is every PosterChanOS machine, and the error
# a person sees is "all ebuilds have been masked" about software their own OS ships.
KEYWORDS="amd64"
RESTRICT="mirror strip"

RDEPEND="
	gui-wm/sway
	x11-base/xwayland
	media-video/pipewire
	media-video/wireplumber
"

# `default` would work -- a .tar.zst is an archive portage knows -- but the tree is unpacked into a
# subdirectory of its own so src_install has one predictable path to copy, whatever the archive's
# top level happens to look like.
src_unpack() {
	mkdir -p "${WORKDIR}/tree" || die
	tar -C "${WORKDIR}/tree" -xaf "${DISTDIR}/${P}.tar.zst" || die "could not unpack the desktop"
	# THE BINARY, NOT AppRun. AppRun is an AppImage artefact -- electron-builder writes it when it
	# wraps the build into an image -- and this archive is that build BEFORE the wrapping, so it has
	# none and never will.
	[[ -f "${WORKDIR}/tree/posterchan-desktop" ]] || die "that archive has no desktop in it"
}

src_install() {
	insinto /opt/posterchan
	doins -r "${WORKDIR}"/tree/.
	# READABLE AND EXECUTABLE BY THE PEOPLE WHO RUN IT. `--appimage-extract` inherits the umask of
	# whatever ran it; installed at 0700 the one directory every session execs from is root-only.
	fperms -R a+rX /opt/posterchan
	fperms 0755 /opt/posterchan/posterchan-desktop
	# doins deliberately installs ordinary files as 0644. The bundled Tor executable is an
	# extraResource, not the package's main binary, so without an explicit mode first-run reaches the
	# Tor choice and fails with `spawn .../resources/tor/tor/tor EACCES`.
	fperms 0755 /opt/posterchan/resources/tor/tor/tor
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
		: "${ELECTRON_OZONE_PLATFORM_HINT:=auto}"
		export ELECTRON_OZONE_PLATFORM_HINT
		if [ -x "$APPDIR/posterchan-desktop" ]; then exec "$APPDIR/posterchan-desktop" "$@"; fi
		exec "$APPDIR/AppRun" "$@"
	WRAP

	# xdg-desktop-portal 1.20+ gates screencast access on a registered, reverse-DNS app id backed
	# by an installed desktop file. This filename must match Electron's package `desktopName`.
	insinto /usr/share/applications
	newins - place.poster.desktop.desktop <<-'DESKTOP'
		[Desktop Entry]
		Type=Application
		Name=PosterChan
		Exec=posterchan
		Icon=posterchan-desktop
		Categories=Network;
		StartupWMClass=PosterChan
	DESKTOP
}
