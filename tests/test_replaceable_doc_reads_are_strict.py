"""A read that feeds a write of the SAME document must be strict.

Run: venv-unified/bin/python -m pytest tests/test_replaceable_doc_reads_are_strict.py

This is the single most repeated data-loss shape in this codebase, and it has a name in half a dozen
comments already: the replaceable-doc wipe. A kind-30078 document is REPLACEABLE — a write does not
append, it replaces the whole thing — so the read-modify-write is the only way to change one. And
`nostr_store._ws_query` answers the same `[]` for "there is no such document" and for "I could not
reach the relay". Merge onto that empty answer and the write deletes everything every other device
had, in one event, with a 200 on the wire and nothing in any log.

`nostr_store.get_doc` says the rule in its own docstring — "Any caller that writes back what it read
should use [strict], so a failed read can't be mistaken for an empty document" — and so does
`list_docs`, and so does `caldav_store.get_items`. It is stated three times and enforced nowhere,
which is why `pcai:drafts` was still reading non-strict months after `pcai:files-index` two hundred
lines above it was fixed. It has already taken out a files index, a mutes list, a follows list and
an uptime history; `scripts/restore_files_index.py` exists because of it.

So: find every read whose result can reach a `put_doc` of the same document, and require `strict`.
Structural, over the AST of the real source — not a grep for the word, which would be satisfied by
`strict=False`.

WHAT THIS DELIBERATELY DOES NOT FLAG. A read in the same function that runs AFTER the write, or in a
branch the write cannot be reached from, is not a read-modify-write: it is the GET half of a
save/load endpoint. Those have their own weakness — an unreachable relay makes them answer "you have
nothing" rather than "I could not ask" — but the fix there is an API decision about what to return,
not a one-word flag, and folding them in here would make this test a list of things nobody is going
to change.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Reader name -> index of the `d_tag` POSITIONAL argument. (port, d_tag, …)
READ_AT = {"get_doc": 1, "list_docs": 1, "get_docs": 1}
# The write. (port, seckey, d_tag, …)
WRITE_AT = {"put_doc": 2}

# READS THAT LOOK LIKE A READ-MODIFY-WRITE AND ARE NOT, each with the reason it is safe.
# Keyed by (file, function, d-tag expression) so that moving a line does not silence it, and adding
# a SECOND read to one of these functions does not inherit the exemption unnoticed — the entry is
# checked for staleness below.
NOT_A_READ_MODIFY_WRITE = {
    ("app/routers/client.py", "_files_index_backup", "$target"):
        "the write is `prev`, the caller's already-validated index — this read only pulls the OLD "
        "slot's `indexSha` so the superseded blob can be aged out, and it is already wrapped in a "
        "try/except that sets `evicted = None`. An unreachable relay expires nothing, which is the "
        "safe direction: it leaks a blob rather than deleting a version the history still promises.",
}


def _fname(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def _dtag(call, idx):
    """A stable identity for the document a call names, or None if it cannot be read statically."""
    if len(call.args) <= idx:
        return None
    a = call.args[idx]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    if isinstance(a, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in a.values)
    if isinstance(a, ast.Name):
        return "$" + a.id
    if isinstance(a, ast.Attribute):
        return "$" + a.attr
    return None


def _is_strict(call):
    for k in call.keywords:
        if k.arg == "strict":
            return isinstance(k.value, ast.Constant) and k.value.value is True
    return False


def _block_paths(func):
    """Map every call in `func` to the chain of statement-blocks enclosing it.

    Two calls in the same block, or a read in a block that ENCLOSES the write's, are on one path
    through the function. A read in the `if` and a write in the `else` are not, and neither is a
    read that the write has already run past — which is what separates a read-modify-write from the
    load half of a save/load endpoint.
    """
    out = {}

    def walk(stmts, path):
        for st in stmts:
            for c in ast.walk(st):
                if isinstance(c, ast.Call):
                    out.setdefault(id(c), path)
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(st, field, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    walk(inner, path + (id(st) + hash(field),))
            for h in getattr(st, "handlers", []) or []:
                walk(h.body, path + (id(h),))

    walk(func.body, ())
    return out


_CACHE = None


def _findings():
    """Memoised: parsing every module under app/ takes ~6s, and three tests ask the same question.
    Without this the pair sat in the suite's twelve slowest, which is a poor trade for a lint."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _scan()
    return _CACHE


def _scan():
    bad, seen_exempt = [], set()
    for dp, dirs, fs in os.walk(os.path.join(ROOT, "app")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in sorted(fs):
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, ROOT)
            with open(p, encoding="utf-8") as fh:
                try:
                    tree = ast.parse(fh.read())
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
                paths = _block_paths(node)
                writes = []
                for c in calls:
                    if _fname(c) in WRITE_AT:
                        t = _dtag(c, WRITE_AT[_fname(c)])
                        if t:
                            writes.append((t, c))
                if not writes:
                    continue
                for c in calls:
                    n = _fname(c)
                    if n not in READ_AT or _is_strict(c):
                        continue
                    t = _dtag(c, READ_AT[n])
                    if not t:
                        continue
                    rp = paths.get(id(c), ())
                    feeds = any(
                        wt == t
                        # the read must be able to reach the write: same block or an outer one …
                        and paths.get(id(w), ())[:len(rp)] == rp
                        # … and must run first.
                        and c.lineno < w.lineno
                        for wt, w in writes)
                    if not feeds:
                        continue
                    key = (rel, node.name, t)
                    if key in NOT_A_READ_MODIFY_WRITE:
                        seen_exempt.add(key)
                        continue
                    bad.append((rel, c.lineno, node.name, n, t))
    return bad, seen_exempt


class ReplaceableDocReadsAreStrict(unittest.TestCase):

    def test_the_scan_still_sees_the_datastore(self):
        """The guard on the guard: rename `put_doc` and this becomes a test of an empty list."""
        n = 0
        for dp, dirs, fs in os.walk(os.path.join(ROOT, "app")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in fs:
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(dp, f), encoding="utf-8") as fh:
                    src = fh.read()
                n += src.count("put_doc(") + src.count("get_doc(")
        self.assertGreater(n, 20, "the datastore scan found almost nothing (%d call sites) — the "
                                  "reader/writer names have moved" % n)

    def test_no_read_modify_write_reads_a_document_loosely(self):
        bad, _ = _findings()
        self.assertEqual(
            [], bad,
            "these reads feed a put_doc of the SAME document without strict=True, so an "
            "unreachable relay reads as an empty document and the write deletes it:\n  "
            + "\n  ".join("%s:%d  %s()  %s(%r)" % (r[0], r[1], r[2], r[3], r[4]) for r in bad)
            + "\nPass strict=True and refuse the write when it raises (the house pattern is "
              "files_index() in app/routers/client.py: log, then 503 'not saved').")

    def test_the_exemptions_are_still_real(self):
        """An exemption whose call site is gone stops describing anything and starts hiding the
        next read that lands on the same name."""
        _, seen = _findings()
        stale = sorted(set(NOT_A_READ_MODIFY_WRITE) - seen)
        self.assertEqual([], stale,
                         "NOT_A_READ_MODIFY_WRITE names call sites that are no longer there: "
                         + ", ".join("%s %s(%s)" % k for k in stale))

    def test_every_exemption_gives_a_reason(self):
        for k, why in NOT_A_READ_MODIFY_WRITE.items():
            self.assertGreater(len(why), 60, "%s is exempted without a real reason" % (k,))


if __name__ == "__main__":
    unittest.main()
