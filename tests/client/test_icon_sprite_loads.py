"""THE SPRITE IS ONE JS TEMPLATE LITERAL, AND A BACKTICK IN A COMMENT ENDS IT.

Every icon in the client comes from `static/js/client/sprite.js`, which holds the whole SVG in a
single backtick-quoted string and injects it at the top of <body>. Its own header comment explains
that a `<use href="#i-name">` pointing at a symbol that is not there renders as blank space with no
error and nothing in the console — so a missing icon is invisible to every kind of testing that is
not a person looking at a screen.

What that header does not say is that the FILE can fail the same way, all at once. A new symbol was
added with an ordinary explanatory comment above it:

    <!-- EVERY ARC FLAG IS SPACE-SEPARATED. `a9 9 0 01-2.6 0` packs the two flags … -->

Those backticks closed the template literal. sprite.js then threw a SyntaxError at parse time, never
ran, and injected nothing — so EVERY icon in the entire client disappeared at once: the desktop
icons, the taskbar, the tray, the tabs. It reached the test machine, where it looked like a styling
bug rather than a dead script, and only a screenshot of the real screen showed it.

Nothing else in the suite could have caught it: every existing sprite test reads the file as TEXT
and asserts symbols are present in it. They all passed. The file was perfectly correct and could not
be loaded.

So this one RUNS it, the way a browser would.
"""
import os
import json
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPRITE = os.path.join(ROOT, "static", "js", "client", "sprite.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class SpriteLoads(unittest.TestCase):
    def _inject(self):
        """Run sprite.js under a stub document and hand back what it injected."""
        js = """
        globalThis.window = globalThis;
        let injected = '';
        globalThis.document = { body: { insertAdjacentHTML: (_p, h) => { injected = h; } } };
        require(%s);
        process.stdout.write(JSON.stringify({ html: injected, ico: typeof globalThis.ICO }));
        """ % json.dumps(SPRITE)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         "sprite.js did not load — EVERY icon in the client is blank when this "
                         "happens, with nothing in any log:\n" + r.stderr[-1500:])
        return json.loads(r.stdout)

    def test_it_actually_runs_and_injects_the_sprite(self):
        out = self._inject()
        self.assertTrue(out["html"].strip().startswith("<svg"),
                        "the sprite injected something that is not an SVG")
        self.assertGreater(out["html"].count("<symbol id=\"i-"), 100,
                           "the sprite injected almost nothing — did the literal end early?")
        self.assertEqual(out["ico"], "function", "window.ICO is what JS-rendered rows use")

    def test_no_backtick_inside_the_literal(self):
        """The mechanism, asserted directly as well as through the run above.

        Running it is the real check; this one exists to name the cause when it fails, because a
        bare SyntaxError three hundred lines into a generated string is not a helpful message and
        the temptation is then to go looking at the SVG."""
        src = open(SPRITE, encoding="utf-8").read()
        start = src.index("var SPRITE = `") + len("var SPRITE = `")
        end = src.index("</defs></svg>`", start)
        body = src[start:end]
        self.assertNotIn("`", body,
                         "a backtick inside the sprite's template literal ends it, and every icon "
                         "in the client vanishes. Write the comment without one.")
        # `${` would be a template substitution — same class of accident, different symptom (it
        # interpolates a variable that does not exist and throws at RUNTIME rather than parse time).
        self.assertNotIn("${", body, "a ${ in the sprite is a template substitution, not markup")

    def test_every_symbol_has_a_unique_id(self):
        """A duplicate id is the other silent one: the SECOND definition is ignored, so an icon
        renders as whatever the first one happened to be — a wrong picture rather than none."""
        out = self._inject()
        ids = out["html"].split('<symbol id="')[1:]
        names = [s.split('"', 1)[0] for s in ids]
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [], "duplicate sprite ids — the later definition never renders")
