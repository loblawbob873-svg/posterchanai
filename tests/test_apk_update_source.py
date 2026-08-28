"""APK updates use one signed, store-independent recovery path."""
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class InstallerSourceTests(unittest.TestCase):
    def test_native_installer_provenance_remains_available_for_diagnostics(self):
        src = open(os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                                "place", "poster", "app", "share", "ShareTargetPlugin.java"),
                   encoding="utf-8").read()
        self.assertIn("public void installer(PluginCall call)", src)
        self.assertIn("getInstallSourceInfo", src)
        self.assertIn("getInstallerPackageName", src)

    def test_every_installer_uses_the_same_signed_direct_upgrade(self):
        app = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
        at = app.index("function applyUpdate()")
        end = app.find("\n  function ", at + 1)
        body = app[at:end if end > at else at + 6000]
        self.assertIn("'/apk'", body)
        self.assertNotIn("_fromZapstore", body)
        self.assertNotIn("P.launch({pkg:_installer})", body.replace(" ", ""))
        self.assertIn("proof-of-rotation", body)
        self.assertIn("install it over this app", body)

    def test_update_check_does_not_wait_for_or_branch_on_installer_identity(self):
        app = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
        start = app.index("async function _checkApkUpdate()")
        end = app.index("function _onNewController()", start)
        check = app[start:end]
        self.assertNotIn("_learnInstaller", check)
        self.assertNotIn("_installer", check)


if __name__ == "__main__":
    unittest.main()
