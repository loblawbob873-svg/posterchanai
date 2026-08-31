"""PosterChan Code's highlighter and its state rules — the SHIPPED code, run under node.

The tokenizer is a pure function of (text, language) and is kept DOM-free at the top of code.js
precisely so this can load and run it rather than matching its source with a regex.

What is worth checking here is not "does python get colours". It is the handful of ways a
regex-alternation scanner goes wrong SILENTLY:

- A rule containing a capturing group shifts every rule after it, so the wrong class is emitted for
  the rest of the language. It still produces plausible-looking coloured output.
- A rule that can match the empty string spins the scanner for ever: the tab freezes with nothing in
  any log. code.js guards it; this proves the guard.
- Rule ORDER decides whether a keyword inside a string gets lit up, which reads as a rendering quirk
  rather than a bug.
- `(?i:…)` is a Python/PCRE inline flag group and a SYNTAX ERROR in JavaScript. Written into a rule
  it makes the whole alternation fail to compile — and because the compile is inside a try (so one
  bad rule cannot take the screen), the language silently loses every colour instead of throwing.
- Output must be ESCAPED. This html is assigned with innerHTML, so a `<script>` in somebody's source
  file is script injection from a file they merely opened.

Run: venv-unified/bin/python -m pytest tests/client/test_code_highlight.py
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CODE = os.path.join(ROOT, "static", "js", "client", "code.js")
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs');
// code.js ends by calling init(), which polls for window.__PC. Give it a window with no bridge:
// init() reschedules itself and never touches the DOM, so the module's pure top half is all that
// runs -- which is the half under test.
global.window = {};
global.setTimeout = () => 0;
require(process.argv[2]);
const H = global.window.PCCodeHL;
const args = JSON.parse(process.argv[3]);
const out = {};
out.languages = Object.keys(H.RULES);
out.samples = {};
for(const [name, spec] of Object.entries(args.samples)) out.samples[name] = H.highlight(spec[0], spec[1]);
// Every rule compiles, and each language's scanner terminates on a difficult buffer.
out.compiles = {};
for(const l of out.languages){
  const t0 = Date.now();
  const r = H.highlight(args.torture, l);
  out.compiles[l] = { ok: typeof r === 'string', ms: Date.now() - t0 };
}
out.langOf = {};
for(const f of args.files) out.langOf[f] = H.langOf(f);
out.hlMax = H.HL_MAX;
console.log(JSON.stringify(out));
"""

# Every construct that has ever broken a scanner like this, in one buffer.
TORTURE = (
    "\n# a comment with 'quotes' and \"doubles\" and a # inside\n"
    "s = 'a string with a # comment and def and 123 inside'\n"
    '"""a docstring\nspanning lines with def class return\n"""\n'
    "def f(a, b=1):\n    return a  # trailing\n"
    "x = {'k': [1, 2.5e10, 0xff], 'v': None}\n"
    "<script>alert(1)</script>\n"
    "if [ -f x ]; then echo $HOME; fi\n"
    "SELECT * FROM t WHERE a='b';\n"
    "/* block */ // line\n"
    "`template ${x}`\n"
    "\n\n\n"
)


@unittest.skipIf(not NODE, "no node on this node")
class TheHighlighter(unittest.TestCase):
    out = None

    @classmethod
    def setUpClass(cls):
        args = {
            "torture": TORTURE,
            "files": ["a.py", "b.sh", "c.js", "d.json", "e.md", "F.JAVA", "g.unknown", "noext"],
            "samples": {
                "py_kw_in_string": ["s = 'def class return'\n", "python"],
                "py_comment_with_quote": ["# it's a comment\nx = 1\n", "python"],
                "py_docstring": ['"""doc"""\nx = 1\n', "python"],
                "py_code": ["def greet(name):\n    return name\n", "python"],
                "escaped": ["x = '<script>alert(1)</script>'\n", "python"],
                "amp": ["a && b\n", "javascript"],
                "sql_lower": ["select a from t\n", "sql"],
                "sql_upper": ["SELECT a FROM t\n", "sql"],
                "json_key_vs_value": ['{"k": "v"}', "json"],
                "bash_var": ["echo $HOME ${X} $1\n", "bash"],
                "bash_code": ["if [ -f x ]; then echo ok; fi\n", "bash"],
                "unknown_lang": ["<b>hi</b>\n", "rustlang"],
                "empty": ["", "python"],
            },
        }
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            h = os.path.join(tmp, "h.js")
            with open(h, "w") as f:
                f.write(HARNESS)
            r = subprocess.run([NODE, h, CODE, json.dumps(args)],
                               capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        cls.out = json.loads(r.stdout.strip().splitlines()[-1])

    # ---- the silent scanner failures ----------------------------------------------------------

    def test_every_language_compiles_and_terminates(self):
        """A rule that fails to compile loses ALL colour for its language without raising (the
        compile is deliberately inside a try). A rule that can match the empty string spins for
        ever. Both are invisible from the output alone, so both are asserted here."""
        for lang, r in self.out["compiles"].items():
            with self.subTest(lang=lang):
                self.assertTrue(r["ok"], lang + " did not return a string")
                self.assertLess(r["ms"], 2000, lang + " took pathologically long — a spinning rule?")

    def test_a_language_with_no_rules_is_escaped_plain_text(self):
        self.assertEqual(self.out["samples"]["unknown_lang"], "&lt;b&gt;hi&lt;/b&gt;\n")

    def test_the_output_is_escaped(self):
        """This html is assigned with innerHTML. Unescaped, opening a file that merely CONTAINS a
        script tag executes it — injection from reading, not from running."""
        got = self.out["samples"]["escaped"]
        self.assertNotIn("<script>", got)
        self.assertIn("&lt;script&gt;", got)
        self.assertNotIn("<b>", self.out["samples"]["unknown_lang"])

    def test_an_ampersand_is_escaped_once(self):
        """Escaping `&` after `<` produces `&amp;lt;` — the classic double-escape, which shows the
        entity itself on screen instead of the character."""
        self.assertIn("&amp;&amp;", self.out["samples"]["amp"])
        self.assertNotIn("&amp;amp;", self.out["samples"]["amp"])

    def test_an_empty_buffer_is_empty(self):
        self.assertEqual(self.out["samples"]["empty"], "")

    # ---- rule order ---------------------------------------------------------------------------

    def test_a_keyword_inside_a_string_is_not_a_keyword(self):
        """Rule ORDER is the whole of this. Strings must be matched before keywords, or `def` lights
        up inside a quoted sentence and the file looks subtly wrong with nothing to explain it."""
        got = self.out["samples"]["py_kw_in_string"]
        self.assertNotIn('t-kw">def', got, "a keyword was lit up inside a string")
        self.assertIn("t-str", got)

    def test_an_apostrophe_in_a_comment_does_not_open_a_string(self):
        """`# it's a comment` — the apostrophe would open a string that runs to the end of the file,
        colouring everything after it, if comments were not matched first."""
        got = self.out["samples"]["py_comment_with_quote"]
        self.assertIn("t-com", got)
        self.assertIn('t-num">1', got, "the rest of the file was swallowed by a string")

    def test_a_docstring_is_one_token(self):
        """The short string rule would match a docstring's first two quotes and stop, leaving the
        body as bare code and the closing quotes as another empty string."""
        got = self.out["samples"]["py_docstring"]
        self.assertIn('<span class="t-str">&quot;&quot;&quot;doc&quot;&quot;&quot;</span>'
                      .replace("&quot;", '"'), got)

    def test_a_json_key_is_told_apart_from_a_value(self):
        got = self.out["samples"]["json_key_vs_value"]
        self.assertIn("t-key", got, "keys and values are painted the same")
        self.assertIn("t-str", got)

    def test_bash_variables_are_found_in_all_three_spellings(self):
        got = self.out["samples"]["bash_var"]
        self.assertEqual(got.count("t-var"), 3, got)

    def test_python_and_bash_keywords_are_visibly_highlighted(self):
        for sample in ("py_code", "bash_code"):
            with self.subTest(sample=sample):
                self.assertIn("t-kw", self.out["samples"][sample])

    def test_sql_keywords_match_in_either_case(self):
        """SQL is the one case-insensitive language here, and `(?i:…)` — the obvious way to say so —
        is a syntax error in JavaScript that would silently un-colour the whole language."""
        self.assertIn("t-kw", self.out["samples"]["sql_lower"])
        self.assertIn("t-kw", self.out["samples"]["sql_upper"])

    # ---- language detection -------------------------------------------------------------------

    def test_the_extension_decides_the_language(self):
        want = {"a.py": "python", "b.sh": "bash", "c.js": "javascript", "d.json": "json",
                "e.md": "markdown", "F.JAVA": "java", "g.unknown": "text", "noext": "text"}
        self.assertEqual(self.out["langOf"], want)

    def test_there_is_a_ceiling_on_what_gets_coloured(self):
        """The scan is one pass over the whole buffer per repaint, and a repaint follows a keystroke.
        Without a ceiling a large file is unusably laggy — a slow editor is a broken editor."""
        self.assertGreater(self.out["hlMax"], 10000)
        self.assertLess(self.out["hlMax"], 2_000_000)


@unittest.skipIf(not NODE, "no node on this node")
class TheStateRules(unittest.TestCase):
    """The rules the header of code.js states, asserted against its source.

    Three separate things repaint this view — #feed being shared, a desktop window being refocused,
    and a monitor handoff REBUILDING THE WINDOW IN A DIFFERENT ELECTRON RENDERER. The first two are
    answered by module state; only the third needs the mirror to localStorage, and it is the one
    that cannot be noticed by testing on one screen."""

    @classmethod
    def setUpClass(cls):
        import re
        with open(CODE, encoding="utf-8") as f:
            raw = f.read()
        cls.raw = raw
        # COMMENTS STRIPPED before asserting what the code does. This file explains at length why it
        # does NOT call `PC.loadModule`, and a naive substring search then fails on the explanation —
        # a test that forbids talking about a mistake as well as making one.
        no_block = re.sub(r"/\*.*?\*/", " ", raw, flags=re.S)
        cls.src = re.sub(r"(?<!:)//[^\n]*", " ", no_block)

    def test_the_caret_and_scroll_are_restored_after_a_repaint(self):
        self.assertIn("function restoreCaret()", self.src)
        self.assertIn("ta.setSelectionRange(s, e)", self.src)
        self.assertIn("ta.scrollTop = d.scroll", self.src)

    def test_the_editor_paints_highlighted_source_behind_the_textarea(self):
        self.assertIn('class="pcc-layer pcc-hl"', self.raw)
        self.assertIn('class="pcc-layer pcc-ta"', self.raw)
        self.assertIn("highlight(d.text, d.lang)", self.src)

    def test_the_state_is_mirrored_outside_this_renderer(self):
        """A monitor handoff destroys the window on one screen and rebuilds it on the other, in a
        different JavaScript context. Module state does not cross; localStorage does."""
        self.assertIn("localStorage.setItem(LSKEY()", self.src)
        self.assertIn("function restore()", self.src)

    def test_explorer_source_control_choice_survives_a_renderer_handoff(self):
        """The selected local folder survived, but its active sidebar destination did not."""
        self.assertIn("gitOpen:S.gitOpen", self.src)
        self.assertIn("S.gitOpen = !!v.gitOpen", self.src)
        switch = self.src[self.src.index("document.querySelectorAll('[data-code-view]'"):]
        switch = switch[:switch.index("on('#pcc-term'")]
        self.assertLess(switch.index("S.gitOpen=git"), switch.index("save(true)"))
        self.assertLess(switch.index("save(true)"), switch.index("paint()"))
        render = self.src[self.src.index("async function render()"):self.src.index("window.PCCode = {")]
        self.assertIn("if(!S.gate&&S.gitOpen)await loadGit()", render)

    def test_the_mirror_is_flushed_on_the_way_out(self):
        """A handoff gives no warning, so a debounce alone loses whatever was typed last."""
        for ev in ("pagehide", "blur", "visibilitychange"):
            with self.subTest(event=ev):
                self.assertIn(ev, self.src)

    def test_typing_never_rewrites_the_textarea(self):
        """Assigning `.value` on every keystroke destroys the caret, the undo stack and any IME
        composition in progress — which makes accented and CJK input impossible."""
        i = self.src.index("function repaintHl()")
        seg = self.src[i:i + 900]
        self.assertNotIn("ta.value =", seg, "the repaint writes back into the textarea")

    def test_the_view_class_is_added_not_assigned(self):
        """`className =` drops the base `.feed` class that supplies flex/overflow, and nothing puts
        it back — the TIMELINE then stops scrolling for the rest of the session after one visit."""
        self.assertIn("feed.classList.add('feed-code')", self.src)
        self.assertNotIn("feed.className =", self.src)

    def test_no_native_dialog_is_used(self):
        """A native confirm/prompt/alert wedges the Electron shell and can be suppressed outright in
        the APK's WebView, where it returns false — so the control silently refuses to work."""
        for bad in ("window.confirm(", "window.alert(", "window.prompt("):
            self.assertNotIn(bad, self.src)

    def test_discard_uses_the_shared_non_native_confirmation(self):
        self.assertIn("ensureAiSession, uiPrompt, uiConfirm } = PC", self.src)
        self.assertIn("if(!await uiConfirm('Discard every change", self.src)

    def test_native_source_control_never_falls_back_to_posterchans_own_repository(self):
        """With no selected folder Electron's process cwd is the application source tree."""
        self.assertIn("if(window.pcHost&&pcHost.pickDirectory&&!S.hostRoot)", self.src)
        self.assertIn("Choose a working directory to use Source Control", self.src)
        guard = self.src.index("if(window.pcHost&&pcHost.pickDirectory&&!S.hostRoot)")
        query = self.src.index("pcHost.gitStatus(S.hostRoot)")
        self.assertLess(guard, query)

    def test_it_does_not_reach_for_a_bridge_helper_that_does_not_exist(self):
        """`PC.loadModule` looks like it should be on the bridge and is not — app.js keeps its loader
        private. Reaching for one is the `PC._fmtBytes is not a function` trap."""
        self.assertNotIn("PC.loadModule", self.src)


class TheViewIsWiredInEveryPlace(unittest.TestCase):
    """A view is registered in SEVEN places and each omission fails differently and quietly.

    This is the shape the repo already knows from commands ("a new command must be added BOTH to
    COMMANDS and to the Telegram lists, or it works in the web UI and falls through to the LLM on
    Telegram"). Here: no route and the screen is blank; missing from VALID and a deep link or a
    restored session silently lands on Home; missing from the title map and the header reads `code`;
    missing from the nav and there is no way in; missing from INSTANCE_VIEWS and a server-less
    desktop build offers a screen that is entirely server; no <script> tag and the module never
    loads; not precached and a cold offline load 404s.
    """

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cls.app = open(os.path.join(root, "static", "js", "client", "app.js"),
                       encoding="utf-8").read()
        cls.html = open(os.path.join(root, "templates", "client.html"), encoding="utf-8").read()
        cls.sw = open(os.path.join(root, "static", "js", "client", "sw.js"), encoding="utf-8").read()

    def test_it_has_a_route(self):
        self.assertIn("renderModuleView('code','code.js','PCCode','render')", self.app)

    def test_a_deep_link_or_a_restored_session_can_reach_it(self):
        i = self.app.index("const VALID = new Set([")
        self.assertIn("'code'", self.app[i:i + 700])

    def test_the_header_has_a_name_for_it(self):
        i = self.app.index("$('#view-title').textContent")
        self.assertIn("code:'PosterChan Code", self.app[i:i + 2000])

    def test_there_is_a_way_in(self):
        """THE SIDEBAR, not the mobile More sheet — and that is the stronger door, not a weaker one.

        This used to look in the ☰ More sheet, which is a PHONE menu, and Code had no sidebar row at
        all. The desktop shell builds its start menu and app grid by reading the sidebar
        (`$$('.sidebar .nav .nav-item[data-view]')` in os.js), so a view with a More entry and no
        sidebar row is reachable on a phone and NOWHERE ELSE — reported as "I don't even see it on
        the web version". An editor for a node's files is desktop work; it belongs beside the
        Terminal, which is the other half of the same job."""
        self.assertIn('data-view="code"', self.html,
                      "no sidebar row: Code is missing from the web sidebar, the desktop start menu "
                      "and the desktop app grid all at once")
        # Beside the Terminal, because they share a node and a gate.
        i = self.html.index('data-view="terminal"')
        self.assertIn('data-view="code"', self.html[i:i + 1400],
                      "the Code row drifted away from the Terminal it belongs with")

    def test_a_server_less_build_does_not_offer_it(self):
        """The whole screen is the server: no workspace to open, no formatter, nothing to save to."""
        i = self.app.index("const INSTANCE_VIEWS = new Set([")
        self.assertIn("'code',", self.app[i:i + 1600])

    def test_the_module_is_actually_loaded(self):
        self.assertIn("/static/js/client/code.js", self.html)

    def test_it_is_cached_with_its_siblings(self):
        self.assertIn("'/static/js/client/code.js'", self.sw)


if __name__ == "__main__":
    unittest.main()
