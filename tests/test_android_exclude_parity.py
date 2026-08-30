"""The phone's exclusion matcher must be a STRICT SUBSET of the browser's.

Run: venv-unified/bin/python -m pytest tests/test_android_exclude_parity.py

`foldersync.js excluder()` is the authority: it decides what syncs, the desktop uses it, and the
engine filters the manifest with it. `Excludes.java` is a deliberately smaller matcher that skips
the obvious directories cheaply so a sweep does not walk Pictures/Old and its twenty thousand
photos. Its own class comment states the contract and the reason:

    if Java excluded something JS did not, the scan would omit paths the engine still has in
    `base`, the engine would read them as "deleted here", and it would delete them from every
    other device. A folder exclusion must never be able to delete anything.

    Missing a skip here costs some directory reads. Adding one costs data.

That asymmetry is the whole design, and it was asserted by two substring checks —
`'p.contains("*")' in ex and "continue" in ex` — which is a test that the file still mentions
wildcards. It cannot see a case-folding difference, a trailing-slash difference, an anchoring
difference, or a bare-name-at-any-depth difference: every way the two matchers can actually diverge.

So this RUNS both. `Excludes.matches` is pure JDK (Calendar/List/Locale/TimeZone, no Android), so
javac and java are enough; the JS side is the shipped `foldersync.js` under node. Both are driven
over the same generated cases and the subset relation is checked case by case. Same instrument as
`tests/test_android_reconcile_parity.py`, which compares the two sync engines decision for decision.

A divergence in the SAFE direction (JS excludes, Java does not) is reported but does not fail: that
is the documented trade, and it costs directory reads rather than files.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDES = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                        "place", "poster", "app", "sync", "Excludes.java")
FOLDERSYNC = os.path.join(ROOT, "static", "js", "client", "foldersync.js")

HAVE_JAVA = shutil.which("javac") is not None and shutil.which("java") is not None
HAVE_NODE = shutil.which("node") is not None

# Patterns a person actually types, and the shapes that separate the two matchers.
PATTERNS = [
    ["Old"],                    # a bare name — matches at any depth
    ["old"],                    # …and case must not change the answer
    ["Pictures/Old"],           # anchored
    ["/Pictures/Old"],          # leading slash
    ["Pictures/Old/"],          # trailing slash
    ["Pictures\\Old"],          # a Windows-style separator
    ["*.tmp"],                  # a wildcard — Java must decline to interpret it
    ["Pictures/**"],            # globstar
    ["node_modules"],
    [""],                       # empty
    ["   "],                    # whitespace only
    ["Old", "*.tmp", "node_modules"],
    ["Übungen"],                # non-ASCII, and the case-folding of it
    ["übungen"],
]

PATHS = [
    "Old", "old", "OLD",
    "Old/a.txt", "old/a.txt",
    "Pictures/Old", "Pictures/Old/a.jpg", "pictures/old/a.jpg",
    "Pictures/OldStuff/a.jpg",          # a PREFIX of the pattern, which must not match
    "Pictures/New/Old/deep/a.jpg",
    "a/b/Old/c.txt",
    "Golden/a.txt",                     # contains "old" as a substring — must not match
    "Scaffold/x", "threshold.txt",
    "notes.tmp", "a/b/notes.tmp", "notes.tmp.bak",
    "node_modules/x/y.js", "app/node_modules/x.js",
    "Übungen/heute.txt", "übungen/heute.txt",
    "Pictures/a.jpg", "Documents/cv.pdf", "a.txt", "",
]

DRIVER = """package place.poster.app.sync;
import java.util.*;
public class ParityDriver {
  public static void main(String[] a) throws Exception {
    java.io.BufferedReader r = new java.io.BufferedReader(
        new java.io.InputStreamReader(System.in, "UTF-8"));
    StringBuilder out = new StringBuilder("[");
    String line; boolean first = true;
    while ((line = r.readLine()) != null) {
      if (line.isEmpty()) continue;
      // path \\t pattern \\u0001 pattern ...   (\\u0002 marks an empty path)
      String[] parts = line.split("\\t", -1);
      String path = parts[0].equals("\\u0002") ? "" : parts[0];
      List<String> pats = new ArrayList<>();
      if (parts.length > 1 && !parts[1].isEmpty())
        for (String p : parts[1].split("\\u0001", -1)) pats.add(p.equals("\\u0002") ? "" : p);
      if (!first) out.append(",");
      first = false;
      out.append(Excludes.matches(path, pats) ? "true" : "false");
    }
    out.append("]");
    System.out.println(out);
  }
}
"""

JS_DRIVER = """
const F = require(%s);
const cases = JSON.parse(require('fs').readFileSync(%s, 'utf8'));
const out = cases.map(([path, pats]) => {
  try { return !!F.excluder(pats)(path); } catch (e) { return 'THREW:' + e.message; }
});
process.stdout.write(JSON.stringify(out));
"""


def _cases():
    return [(p, pats) for pats in PATTERNS for p in PATHS]


@unittest.skipUnless(HAVE_JAVA, "no JDK")
@unittest.skipUnless(HAVE_NODE, "node not installed")
class ExclusionParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cases = _cases()
        cls.tmp = tempfile.mkdtemp(prefix="pcai-excl-")
        pkg = os.path.join(cls.tmp, "place", "poster", "app", "sync")
        os.makedirs(pkg)
        shutil.copy(EXCLUDES, pkg)
        with open(os.path.join(pkg, "ParityDriver.java"), "w", encoding="utf-8") as f:
            f.write(DRIVER)
        r = subprocess.run(["javac", "-encoding", "UTF-8", "-d", cls.tmp,
                            os.path.join(pkg, "Excludes.java"),
                            os.path.join(pkg, "ParityDriver.java")],
                           capture_output=True, text=True, timeout=180)
        cls.compile_err = r.stderr if r.returncode else ""
        cls.java = None
        if r.returncode == 0:
            stdin = "\n".join(
                (p or "") + "\t" + "".join(x or "" for x in pats)
                for p, pats in cls.cases)
            j = subprocess.run(["java", "-cp", cls.tmp, "place.poster.app.sync.ParityDriver"],
                               input=stdin, capture_output=True, text=True, timeout=180)
            if j.returncode == 0:
                cls.java = json.loads(j.stdout.strip())
            else:
                cls.compile_err = j.stderr

        cf = os.path.join(cls.tmp, "cases.json")
        with open(cf, "w", encoding="utf-8") as f:
            json.dump(cls.cases, f)
        drv = os.path.join(cls.tmp, "drv.js")
        with open(drv, "w", encoding="utf-8") as f:
            f.write(JS_DRIVER % (json.dumps(FOLDERSYNC), json.dumps(cf)))
        n = subprocess.run(["node", drv], capture_output=True, text=True, timeout=180)
        cls.js = json.loads(n.stdout) if n.returncode == 0 else None
        cls.js_err = n.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_both_matchers_ran(self):
        """Neither side may silently not answer — a skipped side makes the subset check vacuous."""
        self.assertIsNotNone(self.java, "the Java matcher did not run:\n" + self.compile_err[:3000])
        self.assertIsNotNone(self.js, "the JS matcher did not run:\n" + (self.js_err or "")[:3000])
        self.assertEqual(len(self.cases), len(self.java))
        self.assertEqual(len(self.cases), len(self.js))
        self.assertNotIn(True, [isinstance(x, str) for x in self.js],
                         "foldersync.js excluder() threw on a case")

    def test_the_cases_actually_exercise_both_answers(self):
        """A generated set that is all-false would satisfy the subset relation trivially."""
        self.assertGreater(sum(1 for x in self.js if x is True), 20,
                           "the JS matcher excluded almost nothing — the cases are not exercising it")
        self.assertGreater(sum(1 for x in self.java if x is True), 5,
                           "the Java matcher excluded almost nothing — the cases are not exercising it")

    def test_java_never_excludes_what_javascript_keeps(self):
        """THE RULE. A path Java skips and JS keeps is missing from the scan while the engine still
        holds it in `base` — read as 'deleted here', and deleted from every other device."""
        bad = [(p, pats, ) for (p, pats), j, s in zip(self.cases, self.java, self.js)
               if j and not s]
        self.assertEqual(
            [], bad,
            "Excludes.java skips paths foldersync.js would sync. Each one is a file the phone "
            "omits from its scan while the engine still has it, which the engine reads as a "
            "deletion and propagates to every other device:\n  "
            + "\n  ".join("path=%r patterns=%r" % (p, pats) for p, pats in bad))

    def test_the_safe_direction_is_reported_but_allowed(self):
        """JS excluding more than Java is the documented trade: it costs directory reads, not
        files. Asserted only so the count cannot quietly become 'everything'."""
        safe = sum(1 for j, s in zip(self.java, self.js) if s and not j)
        self.assertLess(safe, len(self.cases),
                        "Java matched nothing at all that JS matched — it has stopped working, and "
                        "the subset test above passes vacuously when that happens")

    def test_wildcards_are_left_to_javascript(self):
        """Named, because it is the specific rule the old substring assertion was reaching for:
        Java must DECLINE a pattern it cannot be certain about rather than approximate it."""
        for (p, pats), j in zip(self.cases, self.java):
            if any("*" in x or "?" in x for x in pats) and len(pats) == 1:
                self.assertFalse(j, "Excludes.java interpreted the wildcard pattern %r (path %r). "
                                    "Only JS may do that." % (pats, p))


if __name__ == "__main__":
    unittest.main()
