"""PosterChan Code's find / replace / search-in-files logic — the SHIPPED helpers, run under node.

code.js keeps the match finding DOM-free (`window.PCCodeFind`) so this loads the real file and runs
it, rather than matching its source. Each case is a way find-and-replace goes wrong quietly:

- Case-insensitive by default, and Match case / Whole word / Regex each actually change the answer.
- A half-typed regex (`(`) is an ANSWER with an error, never a throw that takes the screen down.
- Plain-text mode escapes regex syntax: searching `a.b` must not match `axb`.
- Empty regex matches (`^`, `x*`) are not counted — "3 of 17" must name matches you can see.
- Replace all with `$1` expands capture groups in regex mode and is LITERAL in plain mode, and it
  replaces exactly the matches that were counted.
- next/previous WRAP at both ends.
- grep reports the right line numbers and absolute offsets, which is what "open the hit with the
  match selected" is built on.

Run: venv-unified/bin/python -m pytest tests/client/test_code_find_replace.py
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
global.window = {};
global.setTimeout = () => 0;
require(process.argv[process.argv.length - 1]);
const F = global.window.PCCodeFind;
if(!F){ console.log(JSON.stringify({missing:true})); process.exit(0); }
const T = 'Foo foo FOO food\nbar(foo) = foo_bar\nfoo.bar axbar a.bar\n';
const out = {};
const r = (q, o) => F.findAll(T, q, o || {}).ranges.map(([s, e]) => T.slice(s, e));
out.ci = r('foo');
out.cs = r('foo', {cs:true});
out.ww = r('foo', {ww:true});
out.wwcs = r('foo', {ww:true, cs:true});
out.lit = r('a.bar');
out.re = r('a.bar', {re:true});
out.bad = F.findAll(T, '(', {re:true});
out.badww = F.findAll(T, '[', {re:true, ww:true});
out.empty = F.findAll(T, '^', {re:true}).ranges.length;
out.star = F.findAll('aaxaa', 'x*', {re:true}).ranges;
out.none = F.findAll(T, '', {}).ranges.length;
out.capped = F.findAll('aaaaa', 'a', {}, 3);
out.noRoom = F.findAll('aaaaa', 'a', {}, 0);
out.ra1 = F.replaceAll('foo(1) foo(22)', 'foo\\((\\d+)\\)', {re:true}, 'bar[$1]');
out.ra2 = F.replaceAll('x=1; y=2', '(\\w)=(\\d)', {re:true}, '$2=$1 $$ $& $<n>');
out.ra3 = F.replaceAll('cost $1 here', '$1', {}, '$&$1');
out.ra4 = F.replaceAll('Foo foo', 'foo', {cs:true}, 'X');
out.ra5 = F.replaceAll('a^b', '^', {re:true}, 'X');
out.ra6 = F.replaceAll('nothing', 'zzz', {}, 'X');
out.ra7 = F.replaceAll('k1 k2', '(?<n>k)(\\d)', {re:true}, '$<n>-$2 $12');
out.one = F.replacementAt('ab ab', 3, '(a)(b)', {re:true}, '$2$1');
const R = [[0,1],[5,6],[9,10]];
out.pick = [F.pick(R,0,1), F.pick(R,3,1), F.pick(R,11,1), F.pick(R,5,-1), F.pick(R,0,-1), F.pick([],0,1)];
out.step = [F.step(0,3,1), F.step(2,3,1), F.step(0,3,-1), F.step(-1,3,1), F.step(-1,3,-1), F.step(0,0,1)];
const G = 'one\n  two foo\nthree\nfoo foo\n';
out.grep = F.grep(G, 'foo', {}, 100).hits.map(h => [h.line, h.s, h.e, h.pre, h.mid, h.post, G.slice(h.s, h.e)]);
out.grepCap = F.grep(G, 'foo', {}, 2);
console.log(JSON.stringify(out));
"""


@unittest.skipIf(not NODE, "no node on this node")
class FindReplaceLogic(unittest.TestCase):
    out = None

    @classmethod
    def setUpClass(cls):
        p = subprocess.run([NODE, "-e", HARNESS, "--", CODE], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr
        cls.out = json.loads(p.stdout.strip().splitlines()[-1])
        assert not cls.out.get("missing"), "code.js does not publish window.PCCodeFind"

    def test_case_and_word_toggles_change_the_answer(self):
        o = self.out
        self.assertEqual(o["ci"], ["Foo", "foo", "FOO", "foo", "foo", "foo", "foo"])
        self.assertEqual(o["cs"], ["foo", "foo", "foo", "foo", "foo"])
        # `food` and `foo_bar` are not the WORD foo; `foo.bar` and `(foo)` are.
        self.assertEqual(o["ww"], ["Foo", "foo", "FOO", "foo", "foo"])
        self.assertEqual(o["wwcs"], ["foo", "foo", "foo"])

    def test_plain_text_escapes_regex_syntax(self):
        self.assertEqual(self.out["lit"], ["a.bar"])
        self.assertEqual(self.out["re"], ["axbar", "a.bar"])

    def test_an_invalid_regex_is_an_answer_not_a_throw(self):
        for k in ("bad", "badww"):
            self.assertEqual(self.out[k]["ranges"], [])
            self.assertTrue(self.out[k]["error"])

    def test_empty_matches_are_not_counted(self):
        self.assertEqual(self.out["empty"], 0)
        self.assertEqual(self.out["star"], [[2, 3]])
        self.assertEqual(self.out["none"], 0)

    def test_no_room_left_means_no_hits_not_no_limit(self):
        """Code review: a search lane with 0 hits of room left passed max=0, which `||` read as
        "no limit" — up to 20,000 extra hits past the 2,000 cap."""
        self.assertEqual(len(self.out["noRoom"]["ranges"]), 0)

    def test_a_capped_search_says_so(self):
        self.assertEqual(len(self.out["capped"]["ranges"]), 3)
        self.assertTrue(self.out["capped"]["capped"])

    def test_replace_all_expands_groups_in_regex_mode_only(self):
        o = self.out
        self.assertEqual(o["ra1"], {"text": "bar[1] bar[22]", "count": 2, "error": ""})
        # $2/$1 swap, $$ is a dollar, $& the match, an unknown named group stays literal.
        self.assertEqual(o["ra2"]["text"], "1=x $ x=1 $<n>; 2=y $ y=2 $<n>")
        self.assertEqual(o["ra2"]["count"], 2)
        # PLAIN mode: `$1` is two characters to find and `$&$1` is four characters to insert.
        self.assertEqual(o["ra3"], {"text": "cost $&$1 here", "count": 1, "error": ""})
        self.assertEqual(o["ra4"]["text"], "Foo X")
        # `$12` with two groups is `$1` then "2" — what String.replace does.
        self.assertEqual(o["ra7"]["text"], "k-1 k2 k-2 k2")
        self.assertEqual(o["one"], "ba")

    def test_replace_all_only_replaces_what_was_counted(self):
        self.assertEqual(self.out["ra5"], {"text": "a^b", "count": 0, "error": ""})
        self.assertEqual(self.out["ra6"]["count"], 0)
        self.assertEqual(self.out["ra6"]["text"], "nothing")

    def test_next_and_previous_wrap(self):
        # forward from 0 → first, from 3 → second, past the end → WRAPS to first; backward from 5
        # → the one before (first), from the start → WRAPS to last; nothing → -1.
        self.assertEqual(self.out["pick"], [0, 1, 0, 0, 2, -1])
        self.assertEqual(self.out["step"], [1, 0, 2, 0, 2, -1])

    def test_grep_reports_lines_and_offsets_of_the_real_text(self):
        g = self.out["grep"]
        self.assertEqual([h[0] for h in g], [2, 4, 4])
        for h in g:
            self.assertEqual(h[6].lower(), "foo")      # the offsets point at the match itself
            self.assertEqual(h[4], "foo")
        self.assertEqual(g[0][3], "  two ")
        self.assertEqual(g[1][5], " foo")
        self.assertEqual(len(self.out["grepCap"]["hits"]), 2)
        self.assertTrue(self.out["grepCap"]["capped"])


if __name__ == "__main__":
    unittest.main()
