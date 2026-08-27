#!/bin/bash
# Prove the installed PosterChan desktop can execute its bundled Tor before first-run offers it.
set -euo pipefail

ROOT="${PC_INSTALLED_RESOURCES:-/opt/posterchan/resources}"
BIN="$ROOT/tor/tor/tor"
GEO="$ROOT/tor/data/geoip"
GEO6="$ROOT/tor/data/geoip6"

[ -f "$BIN" ] || { echo "installed Tor binary is missing: $BIN" >&2; exit 1; }
[ -x "$BIN" ] || { echo "installed Tor binary is not executable: $BIN" >&2; exit 1; }
[ -s "$GEO" ] || { echo "installed Tor geoip database is missing" >&2; exit 1; }
[ -s "$GEO6" ] || { echo "installed Tor geoip6 database is missing" >&2; exit 1; }

# The Linux expert bundle has no RPATH. tor.js supplies this exact directory at spawn time; exercise
# the same loader path so a package with an executable bit but missing private libraries still fails.
LD_LIBRARY_PATH="$(dirname "$BIN")" "$BIN" --version | grep -q '^Tor version '
echo "Installed bundled Tor is executable with GeoIP data and private libraries"
