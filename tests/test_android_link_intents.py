"""Other apps can hand PosterChan a nostr: link, a mailto: link, or a picture/video/song to open.

Reported 2026-10-15:
  * "Other apps that have functions like 'open in another nostr app' don't show posterchan" (T-Bone, on Android);
  * "other apps with email links or stuff should open in posterchan email";
  * "opening an image from an external file manager should have an option to open a file with posterchan --
    office, image, and videos basically".
Android offers an app for a link or a file only when its manifest declares a VIEW filter for it; ours declared
none for `nostr:`, `mailto:` or media. Covered here, each part RUN rather than read where it can be:
  * home/IntentView.java (compiled and run): link -> launch view, malformed -> "";
  * the manifest declares the three entries, exported, DEFAULT (+BROWSABLE for links), targeting MainActivity;
  * MainActivity derives the launch view from the link on cold start AND on onNewIntent;
  * the SHIPPED phoneshell.js consumeLaunchView hands nostr:/mailto: to app.js's openers (node, stubbed bridge);
  * the SHIPPED app.js exposes those openers and sends a media arrival to Preview.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "mobile", "android", "app", "src", "main")
HOME = os.path.join(APP, "java", "place", "poster", "app", "home")
MANIFEST = os.path.join(APP, "AndroidManifest.xml")
JAVAC, JAVA_BIN, NODE = shutil.which("javac"), shutil.which("java"), shutil.which("node")
A = "{http://schemas.android.com/apk/res/android}"
NPUB = "npub1" + "q" * 58
VIEW, SENDTO = "android.intent.action.VIEW", "android.intent.action.SENDTO"

CASES = {
    "npub": (VIEW, "nostr:" + NPUB),
    "slashes": (VIEW, "nostr://" + NPUB.upper()),
    "nevent-query": (VIEW, "nostr:nevent1" + "q" * 40 + "?relay=x"),
    "junk": (VIEW, "nostr:hello"),
    "not-view": (SENDTO, "nostr:" + NPUB),
    "mailto": (VIEW, "mailto:a@b.example?subject=Hi%20there&body=x"),
    "mailto-sendto": (SENDTO, "mailto:a@b.example"),
    "mailto-ctrl": (VIEW, "mailto:a@b.example?subject=x%0d\nBcc:evil"),
    "https": (VIEW, "https://poster.place/" + NPUB),
    "null": (VIEW, None),
}


@unittest.skipIf(not (JAVAC and JAVA_BIN), "no JDK on this node")
class IntentViewRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        calls = "\n".join('    System.out.println("%s=" + IntentView.of(%s, %s));' % (
            k, json.dumps(a), "null" if d is None else json.dumps(d)) for k, (a, d) in CASES.items())
        drive = "import place.poster.app.home.IntentView;\npublic class Drive { public static void main(String[] x) {\n%s\n}}" % calls
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "Drive.java"), "w").write(drive)
            r = subprocess.run([JAVAC, "-nowarn", "-d", d, os.path.join(HOME, "IntentView.java"), os.path.join(d, "Drive.java")],
                               capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, r.stderr
            run = subprocess.run([JAVA_BIN, "-cp", d, "Drive"], capture_output=True, text=True, timeout=60)
            assert run.returncode == 0, run.stderr
        cls.out = dict(line.split("=", 1) for line in run.stdout.splitlines())

    def test_a_nostr_link_becomes_the_entity_view(self):
        o = self.out
        self.assertEqual(o["npub"], "nostr:" + NPUB)
        self.assertEqual(o["slashes"], "nostr:" + NPUB, "nostr:// and upper case are what some apps send")
        self.assertEqual(o["nevent-query"], "nostr:nevent1" + "q" * 40)
        self.assertEqual((o["junk"], o["not-view"], o["https"], o["null"]), ("", "", "", ""))

    def test_a_mail_link_is_passed_whole_and_a_header_injection_is_refused(self):
        o = self.out
        self.assertEqual(o["mailto"], "mailto:a@b.example?subject=Hi%20there&body=x")
        self.assertEqual(o["mailto-sendto"], "mailto:a@b.example")
        self.assertEqual(o["mailto-ctrl"], "")


def _aliases():
    root = ET.parse(MANIFEST).getroot()
    return {el.get(A + "name"): el for el in root.iter("activity-alias")}


class Manifest(unittest.TestCase):
    def _entry(self, name, scheme=None, mimes=None, actions=(VIEW,), browsable=False):
        el = _aliases().get(name)
        self.assertIsNotNone(el, "no %s entry: Android will never offer PosterChan for it" % name)
        self.assertEqual(el.get(A + "targetActivity"), ".MainActivity")
        self.assertEqual(el.get(A + "exported"), "true")
        self.assertNotEqual(el.get(A + "enabled"), "false")
        f = el.find("intent-filter")
        self.assertEqual({a.get(A + "name") for a in f.findall("action")}, set(actions))
        cats = {c.get(A + "name") for c in f.findall("category")}
        self.assertIn("android.intent.category.DEFAULT", cats, "without DEFAULT an implicit VIEW never matches")
        if browsable:
            self.assertIn("android.intent.category.BROWSABLE", cats, "a link in a browser needs BROWSABLE")
        data = f.findall("data")
        if scheme:
            self.assertEqual({d.get(A + "scheme") for d in data}, {scheme})
        if mimes:
            self.assertEqual({d.get(A + "mimeType") for d in data if d.get(A + "mimeType")}, set(mimes))

    def test_nostr_links(self):
        self._entry(".OpenNostrLink", scheme="nostr", browsable=True)

    def test_mail_links(self):
        self._entry(".OpenMailLink", scheme="mailto", actions=(VIEW, SENDTO), browsable=True)

    def test_pictures_videos_and_songs_open_but_are_not_a_share_target(self):
        self._entry(".OpenMedia", mimes=("image/*", "video/*", "audio/*"))


class MainActivityWiring(unittest.TestCase):
    def test_the_link_reaches_launch_view_on_a_cold_start_and_a_warm_one(self):
        src = open(os.path.join(APP, "java", "place", "poster", "app", "MainActivity.java")).read()
        self.assertIn("IntentView.of(intent.getAction(), intent.getDataString())", src)
        on_create = src[src.index("public void onCreate("):src.index("registerPlugin(ScreenSharePlugin.class)")]
        on_new = src[src.index("public void onNewIntent("):]
        on_new = on_new[:on_new.index("\n    }\n")]
        self.assertIn("launchViewOf(launch)", on_create)
        self.assertIn("launchViewOf(intent)", on_new)


PHONESHELL_RUN = r"""
const fs = require('fs'), vm = require('vm');
const calls = [], toasts = [];
const listeners = {};
const __PC = { openBech32: async v => { calls.push(['bech32', v]); return v.indexOf('bad') < 0; },
               openMailto: v => calls.push(['mail', v]), toast: m => toasts.push(m), switchView: v => calls.push(['view', v]),
               capPlugin: (name) => name === 'HomeScreen' ? Home : null };
let parked = '';
const Home = { consumeLaunchView: async () => { const v = parked; parked = ''; return { view: v }; },
               addListener: (n, f) => { listeners[n] = f; } };
const window = { __PC, Capacitor: { Plugins: { Home } }, addEventListener(){}, localStorage: { getItem: () => null } };
const document = { querySelector: s => s === '#feed' ? {} : null, addEventListener(){}, visibilityState: 'visible',
                   documentElement: { classList: { contains: () => false } } };
const ctx = { window, document, console, setTimeout: (f, ms) => setTimeout(f, Math.min(ms || 0, 5)), clearTimeout,
              localStorage: window.localStorage, location: { origin: 'https://poster.place' } };
ctx.globalThis = ctx;
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
(async () => {
  for (let i = 0; i < 100 && !window.PCPhone; i++) await new Promise(r => setTimeout(r, 5));
  const P = window.PCPhone;
  await P.consumeLaunchView('nostr:NPUB');
  await P.consumeLaunchView('mailto:a@b.example?subject=x');
  await P.consumeLaunchView('nostr:npub1bad');
  await new Promise(r => setTimeout(r, 30));
  console.log(JSON.stringify({ calls, toasts }));
})().catch(e => { console.error(e); process.exit(1); });
""".replace("NPUB", NPUB)


@unittest.skipIf(not NODE, "node not installed")
class Client(unittest.TestCase):
    def test_the_shipped_launch_handler_opens_links_instead_of_treating_them_as_screens(self):
        with tempfile.TemporaryDirectory() as d:
            js = os.path.join(d, "run.cjs")
            open(js, "w").write(PHONESHELL_RUN)
            r = subprocess.run([NODE, js, os.path.join(ROOT, "static/js/client/phoneshell.js")],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIn(["bech32", "nostr:" + NPUB], got["calls"])
        self.assertIn(["mail", "mailto:a@b.example?subject=x"], got["calls"])
        self.assertFalse([c for c in got["calls"] if c[0] == "view" and c[1].startswith(("nostr:", "mailto:"))],
                         ("a link was switched to as if it were a screen name", got))
        self.assertTrue(any("Nostr link" in t for t in got["toasts"]), ("a bad link must say so", got))

    def test_app_js_exposes_the_openers_and_opens_media_in_preview(self):
        src = open(os.path.join(ROOT, "static/js/client/app.js")).read()
        self.assertIn("openBech32, openMailto,", src[src.index("window.__PC = {"):][:400])
        doc = src[src.index("async function _consumeOpenDoc("):]
        doc = doc[:doc.index("\n  }\n")]
        self.assertIn("r.kind === 'media' && r.path", doc)
        self.assertIn("Capacitor.convertFileSrc('file://' + r.path)", doc)
        self.assertIn("PCPreview.open({ name, mime, url, size: r.size })", doc)
        mail = open(os.path.join(ROOT, "static/js/client/mail.js")).read()
        self.assertIn("return this.composeMailto(href);", mail, "one mailto reader for a click and an intent")


if __name__ == "__main__":
    unittest.main()
