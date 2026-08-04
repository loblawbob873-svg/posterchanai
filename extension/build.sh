#!/bin/bash
# Build the Firefox extension into a loadable/uploadable .zip.
#
#   extension/build.sh            -> extension/dist/posterchan-passwords.zip
#
# THE COPY STEP IS THE POINT. vaultcore.js and the nostr bundle are NOT edited here — they are
# copied from the app's own tree every build, so the extension's password generator, TOTP and
# URL-matching are byte-identical to the app's. Two copies that drift is the failure this whole
# arrangement exists to prevent: a generator that quietly omits a character class, or a matcher that
# offers a credential on a domain the app would refuse. tests/test_vault_extension.py fails if the
# checked-in copy stops matching the source.
set -eu
cd "$(dirname "$0")"

SRC=../static/js/client/vaultcore.js
VENDOR=../static/vendor/nostr/nostr.bundle.js

[ -f "$SRC" ] || { echo "missing $SRC"; exit 1; }
[ -f "$VENDOR" ] || { echo "missing $VENDOR"; exit 1; }

mkdir -p vendor dist icons
cp "$SRC" vaultcore.js
cp "$VENDOR" vendor/nostr.bundle.js

# Icons are generated rather than committed as binaries nobody can review in a diff.
if [ ! -f icons/icon-96.png ]; then
  python3 make-icons.py || echo "note: icons not generated (no Pillow) — using whatever is in icons/"
fi

rm -f dist/posterchan-passwords.zip
zip -qr dist/posterchan-passwords.zip \
  manifest.json background.js content.js content.css popup.html popup.js popup.css \
  vaultcore.js vendor icons \
  -x '*.DS_Store'

echo "built dist/posterchan-passwords.zip"
echo
echo "Load it in Firefox:      about:debugging -> This Firefox -> Load Temporary Add-on -> manifest.json"
echo "Firefox for Android:     about:debugging on the desktop, with the phone connected over USB,"
echo "                         or install a signed build from addons.mozilla.org."
