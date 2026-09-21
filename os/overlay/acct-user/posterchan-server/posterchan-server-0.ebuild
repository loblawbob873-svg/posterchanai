# Copyright 2026 PosterChan
# Distributed under the terms of the GNU General Public License v3

EAPI=8

inherit acct-user

DESCRIPTION="The account the bundled PosterChan server runs as (never root, never a person's)"
# Dynamic: a PosterChanOS machine gives numeric ids to people as they sign in, so no fixed number is
# safe to claim here.
ACCT_USER_ID=-1
ACCT_USER_GROUPS=( posterchan-server )
ACCT_USER_HOME=/var/lib/posterchan-server
ACCT_USER_HOME_PERMS=0750
acct-user_add_deps
SLOT="0"
