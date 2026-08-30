"""Every `pcai:` document the client writes must have a DECISION recorded about it.

Run: venv-unified/bin/python -m pytest tests/test_client_private_docs_are_classified.py

Two lists in two different files decide what happens to a client-authored kind-30078 document:

  * `_CARRY_D` (app.js)    — republish it when the user changes their relay set. Miss it and the
                             document stays on a pool the client no longer queries. On a second
                             device, which never held it locally, that is permanent.
  * `_isPinned` (store.js) — exempt it from the newest-N cache eviction that is right for a
                             firehose and wrong for a document only its author can decrypt. Miss it
                             and a few minutes of reading the global feed evicts it, after which the
                             app draws its DEFAULT for that feature.

CLAUDE.md records that "every private doc here has missed one at least once", and the reason it
keeps happening is that adding a document means editing two files, neither of which the new feature
lives in. Neither failure logs anything: the first reads as "my notes are gone", the second as "I
never arranged that desktop".

`tests/test_relay_change_carry.py::test_every_carried_doc_is_also_pinned` already checks one
direction — carried implies pinned. It cannot see the failure that actually happens, because a
document missing from BOTH lists is missing from the list it iterates.

So this asserts the thing that has no owner: every `pcai:` d-tag the client WRITES is classified.
Carried, pinned, or named below with a reason. A new document fails this test until somebody
decides — which is the whole point, and is the same shape as
`test_android_shell_compiles.py::test_a_new_package_is_classified_rather_than_forgotten`.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "static", "js", "client")
APP = os.path.join(CLIENT, "app.js")
STORE = os.path.join(CLIENT, "store.js")

# DOCUMENTS DELIBERATELY LEFT OUT OF BOTH LISTS, each with the reason.
#
# Carrying a document to a relay the user has just added is an act with consequences, so PLAINTEXT
# documents are deliberately not carried: it hands the contents to a relay that may have been added
# on somebody else's recommendation. app.js says so for the news pair; the same judgement covers the
# other two plaintext documents, which is why they are written down here rather than left to be
# rediscovered. None of them is pinned either, and that is safe for the same reason in every case:
# each is small, re-readable from the relay, and its reader refuses to treat an unreachable relay as
# an empty answer — so an evicted copy is a re-fetch, not a loss.
NOT_CLASSIFIED_BECAUSE = {
    "pcai:news-feeds": "plaintext — carrying it hands a full RSS subscription list to a new relay",
    "pcai:news-read": "plaintext — carrying it hands a reading history to a new relay",
    "pcai:client-prefs": "plaintext (app.js says so where the tip presets are written); re-read on "
                         "every boot by _readPrefs, which retries rather than treating an empty "
                         "answer as 'no prefs'",
    "pcai:voices": "plaintext; voicesRead() gates on Relay.ready() and returns null — not [] — for "
                   "an unreachable relay, so an evicted list is re-fetched, never replaced",
    # The board games. Each document is one GAME, shared with an opponent and finished within the
    # hour; there is no library to leave behind and nothing a second device needs to inherit.
    "pcai:blackjack": "per-game state, shared with an opponent, ends with the game",
    "pcai:chesstr": "per-game state, shared with an opponent, ends with the game",
    "pcai:connect4": "per-game state, shared with an opponent, ends with the game",
    "pcai:hangman": "per-game state, shared with an opponent, ends with the game",
    "pcai:holdem": "per-game state, shared with an opponent, ends with the game",
    "pcai:ttt": "per-game state, shared with an opponent, ends with the game",
}


def _code_lines(path):
    """The file with comment LINES dropped — line-based on purpose.

    A `/* … */` regex over app.js is not safe: run non-greedily over the real file it pairs an
    opening delimiter inside a string or a regex literal with a later closing one and deletes
    585,000 characters of live code, `const VOICES_D = 'pcai:voices'` among them. A registry test
    built on that under-reports silently, which is the one failure a registry test must not have.
    """
    out = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for ln in f:
            t = ln.lstrip()
            if t.startswith("//") or t.startswith("*") or t.startswith("/*"):
                continue
            out.append(ln)
    return "".join(out)


def _written_docs():
    """Every `pcai:…` d-tag literal that appears in client code (not in prose)."""
    found = {}
    for name in sorted(os.listdir(CLIENT)):
        if not name.endswith(".js"):
            continue
        src = _code_lines(os.path.join(CLIENT, name))
        for d in re.findall(r"""['"](pcai:[a-z0-9][a-z0-9_-]*)""", src):
            found.setdefault(d, set()).add(name)
    return found


def _carry_patterns():
    src = _code_lines(APP)
    m = re.search(r"const _CARRY_D = \[(.*?)\];", src, re.S)
    assert m, "_CARRY_D is gone from app.js"
    pats = [re.compile(p) for p in re.findall(r"/([^/\n]+)/", m.group(1))]
    assert pats, "no patterns parsed out of _CARRY_D"
    return pats


def _pinned_rules():
    """The d-tag tests inside `_isPinned`, brace-matched — not sliced at the first `return false`,
    which is the opening guard and would yield an EMPTY rule set that passes vacuously."""
    with open(STORE, encoding="utf-8") as f:
        src = f.read()
    i = src.index("function _isPinned")
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                break
        j += 1
    seg = src[i:j]
    rules = [("prefix", v) for v in re.findall(r"startsWith\('([^']+)'\)", seg)]
    rules += [("exact", v) for v in re.findall(r"t\[1\] === '([^']+)'", seg)]
    assert rules, "no d-tag rules parsed out of _isPinned"
    return rules


class PrivateDocsAreClassified(unittest.TestCase):

    def test_the_scan_actually_found_the_documents(self):
        """The guard on the guard. Rename a file, tighten a regex, and this whole test becomes an
        assertion about an empty set — green for ever, and about nothing."""
        docs = _written_docs()
        self.assertGreaterEqual(len(docs), 20,
                                "the pcai: scan found only %d documents — it has stopped seeing "
                                "the client" % len(docs))
        for must in ("pcai:note", "pcai:budget", "pcai:desktop", "pcai:voices"):
            self.assertIn(must, docs, "%s is written by the client but the scan missed it" % must)

    def test_every_document_is_carried_pinned_or_written_down(self):
        carry = _carry_patterns()
        pinned = _pinned_rules()
        unclassified = {}
        for d, files in _written_docs().items():
            if d in NOT_CLASSIFIED_BECAUSE:
                continue
            if any(p.match(d) or p.match(d + ":x") for p in carry):
                continue
            if any((k == "prefix" and d.startswith(v)) or (k == "exact" and d == v)
                   for k, v in pinned):
                continue
            unclassified[d] = sorted(files)
        self.assertEqual(
            {}, unclassified,
            "these documents are written by the client and are in NEITHER _CARRY_D (app.js) nor "
            "_isPinned (store.js): "
            + ", ".join("%s (%s)" % (d, ", ".join(f)) for d, f in sorted(unclassified.items()))
            + ".\nAdd each to _CARRY_D and _isPinned, or to NOT_CLASSIFIED_BECAUSE in this file "
              "with the reason it needs neither.")

    def test_the_exemption_list_has_not_gone_stale(self):
        """An exemption for a document that no longer exists is a note about nothing, and it hides
        the next document that happens to reuse the name."""
        docs = set(_written_docs())
        gone = sorted(d for d in NOT_CLASSIFIED_BECAUSE if d not in docs)
        self.assertEqual([], gone,
                         "NOT_CLASSIFIED_BECAUSE names documents the client no longer writes: "
                         + ", ".join(gone))

    def test_every_exemption_gives_a_reason(self):
        for d, why in NOT_CLASSIFIED_BECAUSE.items():
            self.assertGreater(len(why), 30, "%s is exempted without a real reason" % d)


if __name__ == "__main__":
    unittest.main()
