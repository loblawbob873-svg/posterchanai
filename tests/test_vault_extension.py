"""The Firefox extension ships the SAME vault core as the app.

Run: venv-unified/bin/python -m unittest tests.test_vault_extension

extension/vaultcore.js is a build-time COPY of static/js/client/vaultcore.js. That copy is the whole
reason the extension can be trusted to behave like the app — the password generator, the TOTP
implementation and the "does this credential belong to this site" rule are the same code, and
tests/test_vault_core.py has already checked them against the RFC vectors and the lookalike-domain
cases.

Two copies drift. When they do, the symptoms are the worst kind: a generator that quietly stops
emitting one character class, a TOTP that is right in the app and wrong in the browser, a matcher
that offers a credential on a domain the app would refuse. None of those announce themselves.

So this fails the moment the copy stops matching its source. Fix by re-running extension/build.sh,
never by editing extension/vaultcore.js.
"""
import hashlib
import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "static", "js", "client", "vaultcore.js")
COPY = os.path.join(REPO, "extension", "vaultcore.js")
MANIFEST = os.path.join(REPO, "extension", "manifest.json")


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class ExtensionCore(unittest.TestCase):
    def test_the_copy_is_byte_identical(self):
        self.assertTrue(os.path.exists(COPY), "extension/vaultcore.js missing — run extension/build.sh")
        self.assertEqual(
            _sha(COPY), _sha(SRC),
            "extension/vaultcore.js has drifted from static/js/client/vaultcore.js — "
            "re-run extension/build.sh; never edit the copy")

    def test_the_manifest_loads_the_core_everywhere_it_is_needed(self):
        """The background (sync + TOTP) and the popup (generator) both need it. A missing entry is a
        silently dead feature rather than an error anyone would see."""
        with open(MANIFEST, encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertIn("vaultcore.js", man["background"]["scripts"])
        with open(os.path.join(REPO, "extension", "popup.html"), encoding="utf-8") as fh:
            self.assertIn("vaultcore.js", fh.read())

    def test_it_is_a_firefox_extension(self):
        """browser_specific_settings is what makes it installable on Firefox at all, and the
        gecko_android block is what makes it installable on the phone — which is half the ask."""
        with open(MANIFEST, encoding="utf-8") as fh:
            man = json.load(fh)
        bss = man.get("browser_specific_settings", {})
        self.assertIn("gecko", bss)
        self.assertIn("id", bss["gecko"])
        self.assertIn("gecko_android", bss)

    def test_it_declares_what_data_it_handles(self):
        """Mozilla rejects a submission outright without this key — "The 'data_collection_permissions'
        property is missing" — and it is not something a build step can infer. Declared as
        `authenticationInfo` because that is what a password manager handles and syncs; under-
        declaring is what gets an add-on pulled after the fact, and there is nothing to gain by being
        coy about it here."""
        with open(MANIFEST, encoding="utf-8") as fh:
            man = json.load(fh)
        dcp = man["browser_specific_settings"]["gecko"].get("data_collection_permissions")
        self.assertIsNotNone(dcp, "AMO will reject the submission without this")
        self.assertIn("required", dcp)
        self.assertTrue(dcp["required"], "an empty list is not a declaration; use ['none'] if truly none")

    def test_it_asks_for_no_more_permissions_than_it_uses(self):
        """A password manager asking for permissions it does not use is one nobody should install.
        Every entry here has to be justified by a call in the source."""
        with open(MANIFEST, encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertEqual(sorted(man.get("permissions", [])),
                         ["activeTab", "clipboardWrite", "storage"])
        src = ""
        for f in ("background.js", "popup.js", "content.js"):
            with open(os.path.join(REPO, "extension", f), encoding="utf-8") as fh:
                src += fh.read()
        self.assertIn("storage.local", src)
        self.assertIn("clipboard.writeText", src)
        # No history, no bookmarks, no cookies, no downloads, no webRequest.
        for never in ("history", "bookmarks", "cookies", "downloads", "webRequest", "tabs"):
            self.assertNotIn(never, man.get("permissions", []), f"{never} is not needed")


if __name__ == "__main__":
    unittest.main()


class BundleCompleteness(unittest.TestCase):
    """Both artifacts must ship every file the add-on needs.

    The .zip goes to AMO and the tarball is what people extract for about:debugging. They were
    assembled from two hand-kept lists — one in build.sh, one in the CI workflow — so the signer's
    inject.js/approve.html/approve.js went into the zip and not the tarball. That is not a partial
    add-on: the manifest names inject.js as a content script, so Firefox refuses to load the whole
    thing, and the release's own install instructions pointed at that tarball.
    """

    def test_build_sh_has_one_file_list(self):
        with open(os.path.join(REPO, "extension", "build.sh"), encoding="utf-8") as f:
            s = f.read()
        self.assertIn('zip -qr dist/posterchan-passwords.zip $FILES', s)
        self.assertIn('tar czf dist/posterchan-passwords-unpacked.tar.gz $FILES', s)

    def test_ci_does_not_keep_its_own_list(self):
        wf = os.path.join(REPO, ".github", "workflows", "extension.yml")
        with open(wf, encoding="utf-8") as f:
            s = f.read()
        self.assertNotIn("tar czf dist/posterchan-passwords-unpacked.tar.gz \\", s,
                         "CI is assembling the tarball again instead of using build.sh's")

    def test_every_manifest_file_is_in_the_list(self):
        with open(MANIFEST, encoding="utf-8") as f:
            m = json.load(f)
        with open(os.path.join(REPO, "extension", "build.sh"), encoding="utf-8") as f:
            files = set(f.read().split('FILES="', 1)[1].split('"', 1)[0].replace("\\", "").split())
        want = set()
        for cs in m.get("content_scripts", []):
            want |= set(cs.get("js", [])) | set(cs.get("css", []))
        want |= set(m.get("background", {}).get("scripts", []))
        popup = m.get("action", {}).get("default_popup")
        if popup:
            want.add(popup)
        for war in m.get("web_accessible_resources", []):
            want |= set(war.get("resources", []))
        want |= {"approve.html", "approve.js"}       # opened by the extension, not named in the manifest
        missing = sorted(f for f in want if f not in files and "/" not in f)
        self.assertEqual(missing, [], "not shipped: %s" % missing)
