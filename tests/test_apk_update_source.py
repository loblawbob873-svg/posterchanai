"""Where an APK update sends you depends on who installed the APK.

A Zapstore install and the CI /apk are signed with DIFFERENT KEYS, so the sideload download can
never install over a Zapstore copy — Android refuses the signature. The old row sent every user to
/apk regardless: a Zapstore user tapped Update, a browser opened, a download ran, and the install
failed at the end. The native half answers who installed this build; the client sends Zapstore
installs to Zapstore and everyone else down the direct path that actually works for them.
"""
import os
import unittest

class InstallerSourceTests(unittest.TestCase):
    """A Zapstore install updates THROUGH Zapstore — the store verifies the signature and tracks
    versions, and sideloading /apk over it orphans the install from its store. The native half
    answers who installed the APK; the client branches the update row and the tap on it."""

    def test_the_plugin_answers_who_installed_the_apk(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "mobile", "android", "app", "src", "main", "java",
                                "place", "poster", "app", "share", "ShareTargetPlugin.java"),
                   encoding="utf-8").read()
        self.assertIn("public void installer(PluginCall call)", src)
        self.assertIn("getInstallSourceInfo", src, "API 30+ path missing")
        self.assertIn("getInstallerPackageName", src, "the pre-30 fallback is missing")
        self.assertIn('out.put("installer", who == null ? "" : who)', src,
                      "an unknown installer must answer '' — which keeps the direct-download path")

    def test_the_tap_launches_the_store_app_not_its_website(self):
        """"Mine just tries to open zapstore.dev, not the actual zapstore app" — the native launch
        opens the installer PACKAGE (the answer from installer() is the id); the site is only the
        fallback for a vanished store or an APK too old to carry launch()."""
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        plug = open(os.path.join(root, "mobile", "android", "app", "src", "main", "java",
                                 "place", "poster", "app", "share", "ShareTargetPlugin.java"),
                    encoding="utf-8").read()
        self.assertIn("public void launch(PluginCall call)", plug)
        self.assertIn("getLaunchIntentForPackage", plug)
        self.assertIn('out.put("ok", ok)', plug, "the caller cannot tell launch failed → no fallback")
        app = open(os.path.join(root, "static", "js", "client", "app.js"), encoding="utf-8").read()
        at = app.index("_fromZapstore()){")
        seg = app[at:at + 1400]
        self.assertIn("P.launch({pkg:_installer})", seg.replace(" ", ""))
        self.assertIn("zapstore.dev", seg, "no fallback for a store that cannot be launched")

    def test_the_client_branches_on_zapstore(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        app = open(os.path.join(root, "static", "js", "client", "app.js"), encoding="utf-8").read()
        at = app.index("function applyUpdate()")
        # THE WHOLE FUNCTION, not a fixed byte count. A window of 1600 chars fitted when this was
        # written and stopped fitting the moment the function grew a desktop branch and a paragraph
        # explaining the store launch — so the sideload assertion below silently started reading
        # somebody else's code and went red over a line that was still there. Every source-reading
        # test in this repo has now been bitten by that; bound them to a syntactic edge instead.
        end = app.find("\n  function ", at + 1)
        body = app[at:end if end > at else at + 6000]
        self.assertIn("_fromZapstore()", body)
        self.assertIn("zapstore.dev", body)
        # …and the sideload path survives for everyone else.
        self.assertIn("'/apk'", body)
        self.assertIn("tap to open Zapstore", app, "the update row does not say where the tap goes")


if __name__ == "__main__":
    unittest.main()
