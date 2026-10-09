# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="PosterChan Office on the machine: Collabora CODE, started only when a document is opened"
HOMEPAGE="https://poster.place"
# THE SAME CODE BUILD THE INSTANCE RUNS (officeserver/Collabora_Online.AppImage, buildid 7d478de5),
# republished under a tag that is written once and never touched — the desktop package's lesson: a
# Manifest pins a digest, so a rolling "LATEST" URL verifies today and fails on the next upstream
# release.
SRC_URI="https://github.com/loblawbob873-svg/posterchanai/releases/download/office-code-7d478de5/Collabora_Online-7d478de5.AppImage -> ${P}.AppImage"
S="${WORKDIR}"

LICENSE="MPL-2.0"
SLOT="0"
KEYWORDS="amd64"
RESTRICT="mirror strip binchecks"

# NOT A SERVICE. desktop/office-local.js starts coolwsd on a free loopback port the first time a
# document is opened and stops it after the last one has been closed for ten minutes; nothing here
# installs a unit. It is installed UNPACKED (~860 MB) because the AppImage's own launcher hard-codes
# port 9983 (which the instance's Office uses when the server is enabled on this machine) and
# --appimage-extract-and-run unpacks the whole thing into /tmp on every start.

src_unpack() {
	cp "${DISTDIR}/${P}.AppImage" "${WORKDIR}/code.AppImage" || die
	chmod +x "${WORKDIR}/code.AppImage" || die
	cd "${WORKDIR}" || die
	./code.AppImage --appimage-extract >/dev/null || die "could not unpack CODE"
	rm -f code.AppImage
}

src_install() {
	insinto /opt/posterchan-office
	doins -r "${WORKDIR}"/squashfs-root/.
	fperms -R a+rX /opt/posterchan-office
	# doins installs 0644; everything the launcher and coolwsd exec must be executable again.
	local f
	while IFS= read -r -d '' f; do
		fperms 0755 "/opt/posterchan-office/${f#${WORKDIR}/squashfs-root/}"
	done < <(find "${WORKDIR}/squashfs-root" -type f -perm -u+x -print0)
}
