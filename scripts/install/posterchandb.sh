#!/usr/bin/env bash
# PosterChanDB data directory (docs/POSTERCHANDB.md). Where: $POSTERCHANDB_DIR (set it in
# data/secrets.env), else /var/lib/posterchandb — system state, like /var/lib/postgresql, owned by the
# account the service runs as.
#
# On btrfs the directory gets NOCOW (`chattr -R +C`). PosterChanDB is an append-only log that is
# compacted by rewriting whole segments, so copy-on-write buys it nothing and costs a second copy of
# every flushed block plus fragmentation. +C only takes effect for files created AFTER it is set,
# which is why it is set here, before the store has written anything — and why existing files are
# counted and reported rather than claimed as converted.

posterchandb_dir() {
    local root="${SCRIPT_DIR:-$(pwd)}" v="${POSTERCHANDB_DIR:-}"
    if [ -z "$v" ] && [ -f "$root/data/secrets.env" ]; then
        v="$(sed -n 's/^\(export \)\{0,1\}POSTERCHANDB_DIR=["'"'"']\{0,1\}\([^"'"'"']*\)["'"'"']\{0,1\}$/\2/p' "$root/data/secrets.env" | tail -1)"
    fi
    echo "${v:-/var/lib/posterchandb}"
}

setup_posterchandb_dir() {
    local dir
    dir="$(posterchandb_dir)"
    print_step "PosterChanDB data directory: $dir"
    # owned by the account the service runs as (PosterChanOS's pc-server passes PC_SERVICE_USER)
    local owner="${PC_SERVICE_USER:-$(id -un)}"
    if ! mkdir -p "$dir" 2>/dev/null; then
        sudo mkdir -p "$dir" || { print_warning "could not create $dir — set POSTERCHANDB_DIR in data/secrets.env"; return 0; }
    fi
    if [ "$(stat -c %U "$dir" 2>/dev/null)" != "$owner" ]; then
        chown "$owner": "$dir" 2>/dev/null || sudo chown "$owner": "$dir" || print_warning "could not give $dir to $owner"
    fi
    chmod 700 "$dir" 2>/dev/null || sudo chmod 700 "$dir"
    local fs
    fs="$(stat -f -c %T "$dir" 2>/dev/null)"
    if [ "$fs" != "btrfs" ]; then
        print_success "$dir is on ${fs:-an unknown filesystem} — NOCOW is btrfs-only, nothing to set"
        return 0
    fi
    if ! chattr -R +C "$dir" 2>/dev/null && ! sudo chattr -R +C "$dir"; then
        print_warning "chattr -R +C failed on $dir — the store works, but btrfs will copy-on-write its log"
        return 0
    fi
    if lsattr -d "$dir" 2>/dev/null | awk '{print $1}' | grep -q C; then
        print_success "NOCOW set on $dir (chattr -R +C) — new segments inherit it"
    else
        print_warning "chattr reported success but $dir does not show the C attribute"
        return 0
    fi
    local cow=0 f
    while IFS= read -r f; do
        lsattr "$f" 2>/dev/null | awk '{print $1}' | grep -q C || cow=$((cow + 1))
    done < <(find "$dir" -type f -size +0 2>/dev/null | head -2000)
    if [ "$cow" -gt 0 ]; then
        print_warning "$cow existing file(s) in $dir were written before NOCOW and keep copy-on-write"
        echo "    With the service STOPPED: copy them out and back (cp, not mv), or let compaction rewrite them."
    fi
}
