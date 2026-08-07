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
#
# NEEDS BASH AND PYTHON3, AND NOTHING ELSE. It used to shell out to `zip` and `tar`, which is fine on
# a Linux CI box and is the reason this could not be built on Windows at all: Git for Windows ships
# no `zip`, and `set -e` meant the script died at the first archive line — AFTER the copies but
# BEFORE staging dist/chrome, so you were left with no loadable folder either and an error that named
# a missing tool rather than what to do about it. Python's zipfile/tarfile are in the standard
# library, behave identically on Linux, macOS, WSL and Git Bash, and python3 was already required
# here for the manifest checks below. One implementation, not a fallback chain nobody can test.
set -eu
cd "$(dirname "$0")"

command -v python3 >/dev/null 2>&1 || { echo "build.sh needs python3 on PATH"; exit 1; }

# pack <archive> <member>… — .zip or .tar.gz, decided by the name. Members may be files or
# directories; directories are walked in sorted order so two builds of the same tree agree.
pack () {
  out=$1; shift
  python3 - "$out" "$@" <<'EOF'
import os, sys, tarfile, zipfile

out, members = sys.argv[1], sys.argv[2:]

def files(path):
    if os.path.isfile(path):
        yield path
        return
    for root, dirs, names in os.walk(path):
        dirs.sort()
        for n in sorted(names):
            if n == '.DS_Store':
                continue
            yield os.path.join(root, n)

if out.endswith('.zip'):
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for m in members:
            for f in files(m):
                z.write(f, f)
else:
    with tarfile.open(out, 'w:gz') as t:
        for m in members:
            for f in files(m):
                t.add(f, f)
EOF
}

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
       vaultcore.js bookmarks.js vendor icons"

# Every file the manifest references must actually be in the bundle. Cheap, and it is the check that
# would have caught the above the moment inject.js was added.
python3 - "$FILES" <<'EOF'
import json, os, re, sys
shipped = set(sys.argv[1].split())
m = json.load(open('manifest.json'))
want = set()
# The manifest is not the only thing that names a file. A PAGE does too — popup.html loads popup.js
# and popup.css, approve.html loads approve.js — and none of those appear in manifest.json. The
# desktop app shipped an installer that could not open for exactly this shape of omission (a module
# added, a hand-written packing list not updated), so scan the pages as well as the manifest.
for page in [f for f in shipped if f.endswith('.html')]:
    if os.path.isfile(page):
        html = open(page, encoding='utf-8').read()
        for ref in re.findall(r'(?:src|href)=["\']([^"\':#?]+)["\']', html):
            if not ref.startswith(('http:', 'https:', 'data:', '//')):
                want.add(ref)
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
    sys.exit('the manifest or a page references files the bundle does not ship: ' + ', '.join(missing))
# The pages the extension opens itself are not in the manifest at all — name them here so they
# cannot be dropped either.
for f in ('approve.html', 'approve.js'):
    if f not in shipped:
        sys.exit(f + ' is missing from the bundle; the approval prompt cannot open')
# The CHROME WEB STORE caps `description` at 132 characters and rejects the UPLOAD, not the review —
# "There was a problem uploading your file", after the whole bundle has gone up. AMO has no such cap,
# so a description that sailed through Firefox review (this one shipped at 150) fails Chrome at the
# last step. Caught here, where the artifact is made, rather than in a store's web form.
_desc = m.get('description', '')
if len(_desc) > 132:
    sys.exit('description is %d chars; the Chrome Web Store rejects anything over 132:\n  %s'
             % (len(_desc), _desc))
EOF

rm -rf dist/posterchan-passwords.zip dist/posterchan-passwords.xpi \
       dist/posterchan-passwords-unpacked.tar.gz \
       dist/posterchan-passwords-chrome.zip dist/chrome
pack dist/posterchan-passwords.zip $FILES
# An .xpi IS the .zip under a name Firefox will "Install Add-on From File". Ship it so Firefox
# Nightly / Developer Edition / ESR (with xpinstall.signatures.required=false) can install this
# PERMANENTLY — not just as a temporary add-on that unloads on restart. The AMO-upload artifact is
# the same bytes, kept as .zip.
cp dist/posterchan-passwords.zip dist/posterchan-passwords.xpi
pack dist/posterchan-passwords-unpacked.tar.gz $FILES

# ---- Chrome ------------------------------------------------------------------------------------
# The SAME sources with a GENERATED manifest, never a second checked-in one: two manifests drift, and
# the one that drifts is the one nobody loads day to day. Only the background entry differs — Firefox
# MV3 takes a list of scripts and runs them as an event page, Chrome MV3 takes exactly one service
# worker and REFUSES to load an extension that lists `scripts`. That single key is the whole reason
# this would not install in Chrome; the JS was already portable (every file aliases
# `browser ?? chrome`, and no background script touches the DOM, which a worker does not have).
mkdir -p dist/chrome
for f in $FILES; do cp -r "$f" dist/chrome/; done
cp background-chrome.js dist/chrome/
python3 - <<'EOF'
import json
m = json.load(open('manifest.json'))
m.pop('browser_specific_settings', None)          # Firefox-only; Chrome warns on it
m['background'] = {'service_worker': 'background-chrome.js'}
m['minimum_chrome_version'] = '111'                # "world": "MAIN" landed in 111
# NIP-07 goes into the PAGE'S WORLD DIRECTLY on Chrome. Firefox has to smuggle inject.js in as an
# inline <script> built by content.js, because a `src` to the extension leaks a per-install UUID —
# a real supercookie there. Chrome supports registering a content script with world MAIN, which is
# strictly better: no inline script for a site's CSP to refuse, no injected node, and Chrome's
# extension id is identical for every install, so it fingerprints the extension and not the user.
# inject.js self-invokes when it finds itself in a world without chrome.runtime.id.
cs = m['content_scripts'][0]
cs['js'] = [f for f in cs['js'] if f != 'inject.js']
m['content_scripts'] = [
    {**cs},
    {'matches': cs['matches'], 'js': ['inject.js'], 'run_at': 'document_start',
     'all_frames': cs.get('all_frames', True), 'world': 'MAIN'},
]
with open('dist/chrome/manifest.json', 'w') as fh:
    json.dump(m, fh, indent=2)
    fh.write('\n')
EOF
# Named members, not `.` — an archiver that stores `./manifest.json` gives a store an extension whose
# manifest is not at the root, which is refused at upload with no useful message.
( cd dist/chrome && pack ../posterchan-passwords-chrome.zip $FILES background-chrome.js )

echo "built dist/posterchan-passwords.zip, dist/posterchan-passwords-unpacked.tar.gz"
echo "  and dist/posterchan-passwords-chrome.zip (+ dist/chrome/ to load unpacked)"
echo
echo "Load it in Chrome:       chrome://extensions -> Developer mode -> Load unpacked -> extension/dist/chrome"
echo "                         (or drag dist/posterchan-passwords-chrome.zip onto that page)"
echo "Load it in Firefox:      about:debugging -> This Firefox -> Load Temporary Add-on -> manifest.json"
echo "Firefox for Android:     about:debugging on the desktop, with the phone connected over USB,"
echo "                         or install the signed build (sideloading is not possible there):"
echo "                         https://addons.mozilla.org/firefox/addon/posterchan-passwords/"
echo
# "Duplicate add-on ID" from AMO is not a problem with the build: it means Submit a New Add-on was
# used for an ID that already has a listing. There is one listing per ID, forever, and every build
# after the first is a VERSION of it — including while an earlier version is still in review.
echo "AMO:                     Developer Hub -> your add-on -> Upload New Version."
echo "                         NOT 'Submit a New Add-on' — the ID ($(python3 -c "import json;print(json.load(open('manifest.json'))['browser_specific_settings']['gecko']['id'])")) already has"
echo "                         a listing, and re-submitting it is the 'duplicate add-on ID' error."
echo "                         The version must be higher than anything submitted before: $(python3 -c "import json;print(json.load(open('manifest.json'))['version'])")"
