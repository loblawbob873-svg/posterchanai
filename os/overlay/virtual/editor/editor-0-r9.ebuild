# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="Virtual for the system editor: on PosterChanOS, pvim"
SLOT="0"
KEYWORDS="amd64"

# PosterChanOS's editor is pvim, and only pvim. Gentoo's virtual/editor (0-r8) accepts any of thirty
# editors and portage satisfies it with the first, nano -- so every stage3 arrives with nano installed
# and @system keeps it. A higher revision here, naming pvim alone, is what lets a fresh install carry no
# other editor. Somebody who wants vim, neovim or nano simply emerges it; nothing here forbids them.
RDEPEND="app-editors/pvim"
