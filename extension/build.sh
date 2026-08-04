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

# ONE list, both artifacts. The .zip goes to AMO and the tarball is what people extract for
# about:debugging, and they were maintained separately — in build.sh and again in the CI workflow —
# so the signer's three new files went into the zip and not the tarball. That is not a partial
# bundle, it is an UNLOADABLE one: the manifest names inject.js as a content script, so Firefox
# rejects the whole add-on rather than quietly running without it. Anyone following the release's
# own install instructions got that.
FILES="manifest.json background.js content.js content.css inject.js \
       popup.html popup.js popup.css approve.html approve.js \
       vaultcore.js vendor icons"

# Every file the manifest references must actually be in the bundle. Cheap, and it is the check that
# would have caught the above the moment inject.js was added.
python3 - "$FILES" <<'EOF'
import json, os, sys
shipped = set(sys.argv[1].split())
m = json.load(open('manifest.json'))
want = set()
for cs in m.get('content_scripts', []):
    want |= set(cs.get('js', [])) | set(cs.get('css', []))
for k in ('background',):
    want |= set(m.get(k, {}).get('scripts', []))
for p in (m.get('action', {}).get('default_popup'),):
    if p: want.add(p)
for war in m.get('web_accessible_resources', []):
    want |= set(war.get('resources', []))
missing = sorted(f for f in want if f not in shipped and not os.path.dirname(f))
if missing:
    sys.exit('the manifest references files the bundle does not ship: ' + ', '.join(missing))
# The pages the extension opens itself are not in the manifest at all — name them here so they
# cannot be dropped either.
for f in ('approve.html', 'approve.js'):
    if f not in shipped:
        sys.exit(f + ' is missing from the bundle; the approval prompt cannot open')
EOF

rm -f dist/posterchan-passwords.zip dist/posterchan-passwords-unpacked.tar.gz
zip -qr dist/posterchan-passwords.zip $FILES -x '*.DS_Store'
tar czf dist/posterchan-passwords-unpacked.tar.gz $FILES

echo "built dist/posterchan-passwords.zip and dist/posterchan-passwords-unpacked.tar.gz"
echo
echo "Load it in Firefox:      about:debugging -> This Firefox -> Load Temporary Add-on -> manifest.json"
echo "Firefox for Android:     about:debugging on the desktop, with the phone connected over USB,"
echo "                         or install a signed build from addons.mozilla.org."
echo
# "Duplicate add-on ID" from AMO is not a problem with the build: it means Submit a New Add-on was
# used for an ID that already has a listing. There is one listing per ID, forever, and every build
# after the first is a VERSION of it — including while an earlier version is still in review.
echo "AMO:                     Developer Hub -> your add-on -> Upload New Version."
echo "                         NOT 'Submit a New Add-on' — the ID ($(python3 -c "import json;print(json.load(open('manifest.json'))['browser_specific_settings']['gecko']['id'])")) already has"
echo "                         a listing, and re-submitting it is the 'duplicate add-on ID' error."
echo "                         The version must be higher than anything submitted before: $(python3 -c "import json;print(json.load(open('manifest.json'))['version'])")"
