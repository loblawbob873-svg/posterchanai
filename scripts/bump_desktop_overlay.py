#!/usr/bin/env python3
"""Point the Gentoo overlay at the newest published desktop build.

    venv-unified/bin/python scripts/bump_desktop_overlay.py            # newest
    venv-unified/bin/python scripts/bump_desktop_overlay.py 1.0.831    # a specific one
    venv-unified/bin/python scripts/bump_desktop_overlay.py --check    # report, change nothing

WHY THIS IS A SCRIPT. The desktop's version is `1.0.<github run number>`, so it bumps on EVERY push
— the overlay is behind again within minutes of anybody touching the client. Keeping it current by
hand means: find the newest tag, download 130 MB, compute BLAKE2B and SHA512, rename the ebuild,
rewrite the Manifest. That got done wrong three times (1.0.818, 1.0.825, 1.0.828), and each time
`emerge posterchan-desktop` simply could not run.

WHAT IT WILL NOT DO IS GUESS. The digests come from the bytes, and the SHA512 is cross-checked
against the .sha512 GitHub publishes beside the tarball — if those disagree the download is wrong
and nothing is written. A Manifest is a promise about exact bytes; a wrong one fails at emerge time
on somebody else's machine.

Being a version or two behind is FINE now and no longer breaks anything: the ebuild fetches
`desktop-v<version>`, a tag CI writes once and never touches. This is for when you want PosterChanOS
machines to get a newer desktop, not something to run on every commit.
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchan-desktop")
REPO = "loblawbob873-svg/posterchanai"
BASE = f"https://github.com/{REPO}/releases/download"


def _current():
    names = [f for f in os.listdir(PKG) if f.endswith(".ebuild")]
    if len(names) != 1:
        sys.exit(f"FAIL  expected one ebuild, found {names}")
    return names[0][len("posterchan-desktop-"):-len(".ebuild")], names[0]


def _head_sha():
    out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    return out.stdout.strip() if out.returncode == 0 else ""


def _releases():
    """Every desktop-v release with the commit it was BUILT FROM."""
    # `gh release list --json targetCommitish` is not a thing — that field only exists on the REST
    # payload (and on `release view`). Asking the API once is also one round trip instead of sixty.
    out = subprocess.run(["gh", "api", f"repos/{REPO}/releases?per_page=100", "--paginate"],
                         capture_output=True, text=True, timeout=180)
    if out.returncode:
        sys.exit(f"FAIL  could not list releases: {out.stderr.strip()[:200]}")
    rows = []
    for chunk in re.findall(r"\[.*?\]\s*(?=\[|$)", out.stdout, re.S) or [out.stdout]:
        try:
            data = json.loads(chunk)
        except Exception:
            continue
        for r in data if isinstance(data, list) else []:
            m = re.fullmatch(r"desktop-v(\d+\.\d+\.\d+)", str(r.get("tag_name") or ""))
            if m:
                rows.append((m.group(1), str(r.get("target_commitish") or "")))
    return rows


def _tag_for_commit(sha):
    """THE BUILD MADE FROM THIS COMMIT — not the newest one that happens to exist.

    This is the whole fix. `_newest_tag()` answers "the highest version published so far", and
    `sync.sh` calls this script BEFORE it pushes — so the newest release is always the PREVIOUS
    commit's build and the overlay shipped one deploy behind, for ever. Measured: emerge installed a
    package correctly named 1.0.1326 whose bundled client was 63ccd0de, while CI had already
    published 1.0.1327 from the commit being deployed. Every fix in that deploy was absent from
    PosterChanOS, with a version number that looked right. "Why does desktop not work right then!"

    Returns None when CI has not published for this commit YET, which is a real and ordinary state —
    the caller must say so rather than silently falling back to something older.
    """
    if not sha:
        return None
    for version, commit in _releases():
        if commit and commit.startswith(sha[:12]) or sha.startswith((commit or "x")[:12]):
            return version
    return None


def _newest_tag():
    """The highest desktop-v<version> RELEASE — not `desktop-latest`, whose assets are deleted and
    re-created on every build."""
    out = subprocess.run(["gh", "release", "list", "--repo", REPO, "--limit", "60",
                          "--json", "tagName"], capture_output=True, text=True, timeout=120)
    if out.returncode:
        sys.exit(f"FAIL  could not list releases: {out.stderr.strip()[:200]}")
    vers = []
    for r in json.loads(out.stdout or "[]"):
        m = re.fullmatch(r"desktop-v(\d+\.\d+\.\d+)", r.get("tagName", ""))
        if m:
            vers.append(m.group(1))
    if not vers:
        sys.exit("FAIL  no desktop-v* releases — has the workflow published one yet?")
    return max(vers, key=lambda v: [int(x) for x in v.split(".")])


def _fetch(url):
    with urllib.request.urlopen(url, timeout=300) as r:
        return r.read()


def _audit_payload(blob):
    """Refuse a validly checksummed desktop that omitted the core Concord surface."""
    with tempfile.TemporaryDirectory(prefix="pc-desktop-audit-") as td:
        archive = os.path.join(td, "desktop.tar.zst")
        with open(archive, "wb") as fh:
            fh.write(blob)
        unpack = os.path.join(td, "unpack")
        os.mkdir(unpack)
        got = subprocess.run(
            ["tar", "-C", unpack, "-xaf", archive, "--wildcards",
             "*/resources/app.asar", "*/resources/tor/tor/tor"],
            capture_output=True, text=True,
        )
        if got.returncode:
            raise RuntimeError("desktop tarball has no resources/app.asar: " + got.stderr.strip()[:160])
        asar = next((os.path.join(root, "app.asar") for root, _dirs, files in os.walk(unpack)
                     if "app.asar" in files), None)
        if not asar:
            raise RuntimeError("desktop tarball extracted without app.asar")
        tor = next((os.path.join(root, "tor") for root, _dirs, files in os.walk(unpack)
                    if "tor" in files and root.endswith(os.path.join("resources", "tor", "tor"))), None)
        if not tor:
            raise RuntimeError("desktop tarball has no bundled Tor executable")
        if not os.stat(tor).st_mode & stat.S_IXUSR:
            raise RuntimeError("desktop tarball mode-stripped bundled Tor; first-run would fail EACCES")
        with open(asar, "rb") as fh:
            payload = fh.read()
        start = payload.find(b'{"files":')
        if start < 0:
            raise RuntimeError("app.asar has no readable file index")
        tree, _end = json.JSONDecoder().raw_decode(payload[start:].decode("latin1"))

        def present(path):
            node = tree
            for part in path.split("/"):
                node = (node.get("files") or {}).get(part) if isinstance(node, dict) else None
                if node is None:
                    return False
            return True

        required = (
            "www/static/css/concord.css", "www/static/js/client/concord.js",
            "www/static/js/client/cord-reader.js", "www/static/js/client/cord-protocol.js",
            "www/static/js/client/code.js", "www/static/js/client/hostfiles.js",
            "www/static/js/client/preview.js",
            "www/static/css/client.css",
        )
        missing = [path for path in required if not present(path)]
        # Concord communities now live inside Messages.  Keep auditing both halves of the
        # unified surface so a stale package cannot silently ship either the old standalone
        # launcher or a Messages build with communities omitted.
        if b'data-view="messages"' not in payload:
            missing.append('index.html Messages navigation entry')
        if b'messages-communities' not in payload or b'messages-direct' not in payload:
            missing.append('unified Messages direct/community tabs')
        # These are behavior-bearing package markers, not cosmetic labels.  Checking the source tree
        # did not catch several releases whose generated app.asar silently lagged behind it.
        markers = {
            b"openHostFile": "desktop local-file bridge",
            b"await _withModule('code.js', 'PCCode')": "cold-start Files to Code loader",
            b"feed.classList.toggle('feed-code', VIEW==='code')": "Code/Terminal layout isolation",
            b"gitAct('restore'": "Code per-file discard/restore",
            b"openSyncCodeFile": "synced-folder Code routing",
            b"openSyncOfficeFile": "synced-folder Office routing",
            b"PCPreview": "built-in file Preview",
            b".files-grid:not(.details) .file-card.enc{border-color:transparent": "borderless encrypted file tiles",
            b".osw-slot.feed-term,.osw-slot.feed-code": "stable parked Terminal/Code sizing",
        }
        missing.extend(label for marker, label in markers.items() if marker not in payload)
        if missing:
            raise RuntimeError("desktop payload is missing required runtime surfaces: " + ", ".join(missing))


def main():
    args = [a for a in sys.argv[1:]]
    check_only = "--check" in args
    args = [a for a in args if not a.startswith("-")]
    for_commit = "--for-commit" in sys.argv
    cur, cur_file = _current()
    head = _head_sha()

    if args:
        want = args[0]
    elif for_commit:
        want = _tag_for_commit(head)
        if not want:
            print(f"overlay pins : {cur}")
            print(f"WAITING  no desktop build published for {head[:8]} yet — CI is still running, or "
                  "its desktop job did not run for this commit.")
            print("         PosterChanOS will keep installing the PREVIOUS bundle until it is.")
            print("         Re-run this once the 'Desktop apps' workflow for that commit is green.")
            return 3
    else:
        want = _newest_tag()

    print(f"overlay pins : {cur}")
    print(f"newest build : {want}")
    built_from = dict((v, c) for v, c in _releases()).get(want, "")
    if head and built_from and not (built_from.startswith(head[:12]) or head.startswith(built_from[:12])):
        print(f"note         {want} was built from {built_from[:8]}, not from HEAD ({head[:8]}) — "
              "the bundle it installs is that commit's client, whatever the version number says")
    if check_only:
        if cur != want:
            print(f"BEHIND  run without --check to move it to {want}")
            return 1

    name = f"PosterChan-{want}-linux-x64.tar.zst"
    url = f"{BASE}/desktop-v{want}/{name}"
    print(f"downloading  {url}")
    try:
        blob = _fetch(url)
    except Exception as e:
        sys.exit(f"FAIL  {want} has no per-version release ({e}) — CI publishes desktop-v<version>; "
                 "a build from before that change has only the rolling tag, which is deleted.")

    sha512 = hashlib.sha512(blob).hexdigest()
    blake2b = hashlib.blake2b(blob).hexdigest()
    try:
        published = _fetch(url + ".sha512").decode().split()[0].strip()
    except Exception:
        published = ""
    if published and published != sha512:
        sys.exit("FAIL  the download does not match the .sha512 GitHub published beside it — "
                 "writing this Manifest would fail at emerge time on somebody else's machine")
    try:
        _audit_payload(blob)
    except Exception as e:
        sys.exit(f"FAIL  refusing to publish incomplete desktop payload: {e}")
    print(f"size {len(blob)}  sha512 verified against the published checksum"
          if published else f"size {len(blob)}  (no published .sha512 to cross-check)")

    if cur == want:
        print("OK  already current; published payload audited")
        return 0

    new_file = f"posterchan-desktop-{want}.ebuild"
    subprocess.run(["git", "mv", os.path.join(PKG, cur_file), os.path.join(PKG, new_file)],
                   cwd=ROOT, check=True)
    with open(os.path.join(PKG, "Manifest"), "w", encoding="utf-8") as fh:
        fh.write(f"DIST posterchan-desktop-{want}.tar.zst {len(blob)} "
                 f"BLAKE2B {blake2b} SHA512 {sha512}\n")
    print(f"OK  overlay now pins {want}")
    print("    run: venv-unified/bin/python -m pytest tests/test_gentoo_overlay_pins_resolve.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
