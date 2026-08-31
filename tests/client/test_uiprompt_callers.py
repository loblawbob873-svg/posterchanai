"""`uiPrompt(message, opts)` takes an OPTIONS OBJECT, and nine callers still passed positionals.

Reported as "Files has no ability to rename files anymore". It renamed fine — the box just opened
EMPTY. `renameBlob` called `uiPrompt('Rename', cur, cur)` from an older signature where the second
and third arguments were the value and the placeholder. The current one reads `opts.value`, so a
string second argument silently supplies nothing: the field is blank, the current name is gone, and
the dialog looks like it has forgotten which file you clicked.

Nine sites had drifted the same way — Files rename, playlist create and rename, short titles, voice
names, and BOTH folder-naming prompts in Folder Sync, where the lost value was the guessed folder
name the user is meant to accept. None of them threw, none logged, and every one of them quietly
dropped the one piece of information the prompt existed to pre-fill.

An options object cannot be checked by the callee — `opts.value` on a string is undefined, which is
indistinguishable from "no default wanted" — so it is checked here instead.
"""
import re
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[2] / "static" / "js" / "client"
def _call_args(src, open_paren):
    """The text between a call's parentheses, tracking nesting AND string literals.

    An earlier version grabbed a fixed 400 characters and split on the first top-level comma, which
    ran past the end of short calls and reported commas belonging to unrelated code — a scanner that
    invents findings is worse than none. This walks to the matching close paren.
    """
    depth, i, quote = 0, open_paren, None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2; continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return src[open_paren + 1:i]
        i += 1
    return ""


def _split_top_level(text):
    out, depth, quote, cur = [], 0, None, ""
    for i, ch in enumerate(text):
        if quote:
            cur += ch
            if ch == "\\":
                continue
            if ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch; cur += ch; continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur); cur = ""; continue
        cur += ch
    if cur.strip():
        out.append(cur)
    return out


class EveryCallerPassesOptions(unittest.TestCase):
    def test_no_caller_passes_a_bare_value_where_options_belong(self):
        bad = []
        for path in sorted(CLIENT.glob("*.js")):
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r"uiPrompt\(", src):
                if src[max(0, m.start() - 9):m.start()].rstrip().endswith("function"):
                    continue
                args = _split_top_level(_call_args(src, m.end() - 1))
                if len(args) < 2:
                    continue                       # message only is fine
                second = args[1].strip()
                if second.startswith("{"):
                    continue                       # the options object, which is the contract
                bad.append("%s: uiPrompt(%s, %s)"
                           % (path.name, args[0].strip()[:40], second[:40]))
        self.assertEqual([], bad,
                         "these pass a positional value where uiPrompt wants {value, placeholder, "
                         "ok, cancel, password}. `opts.value` on a string is undefined, so the "
                         "prompt opens blank and silently loses its default:\n  " + "\n  ".join(bad))

    def test_the_signature_this_guards_has_not_changed(self):
        """If uiPrompt ever takes positionals again, this file is asserting the wrong contract."""
        app = (CLIENT / "app.js").read_text(encoding="utf-8")
        self.assertIn("function uiPrompt(message, opts={})", app,
                      "uiPrompt's signature moved — re-derive what callers should pass")
        self.assertIn("opts.value", app, "the default no longer comes from opts.value")


if __name__ == "__main__":
    unittest.main()
