"""Every module-level import in `app/` is a package the installer actually installs.

Run: venv-unified/bin/python -m pytest tests/test_requirements_cover_the_imports.py

An undeclared module-level import is not a degraded feature — it is a node that does not START.
`sync.sh` deploys CODE, not dependencies (CLAUDE.md), so the failure lands on every node at once,
after the push, as an ImportError in a service that was healthy a second earlier.

MODULE LEVEL ONLY, and that is the whole rule. This codebase deliberately imports heavy optional
things (torch, paramiko, diffusers, acestep, radicale, searx) INSIDE functions or behind try/except,
so the feature is absent rather than fatal on a node that never installed it. Those are correct and
are not asserted here — an allowlist of "optional" packages is exactly the kind of list that goes
stale and then passes against a real break. What cannot be optional is an import that runs at import
time, so that is what is checked, and the rule needs no maintenance.
"""
import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQS = ("requirements.txt", "requirements-nostr.txt")

# Where the import name and the distribution name genuinely differ. Only real cases: a guess here
# would hide a missing package rather than report it.
DIST = {
    "jose": "python-jose", "PIL": "pillow", "yaml": "pyyaml", "fitz": "pymupdf",
    "dotenv": "python-dotenv", "multipart": "python-multipart", "jwt": "pyjwt",
    "psycopg2": "psycopg2-binary", "bs4": "beautifulsoup4", "dateutil": "python-dateutil",
    "magic": "python-magic", "cv2": "opencv-python", "OpenSSL": "pyopenssl",
    "Crypto": "pycryptodome", "telegram": "python-telegram-bot",
}
FIRST_PARTY = {"app", "botframework", "scripts", "tests", "run"}


def _norm(name):
    """PEP 503. `edge_tts` the module is `edge-tts` the distribution, and comparing them literally
    reports a package that has been in requirements.txt all along as missing — which is how a check
    like this teaches people to ignore it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared():
    out = set()
    for r in REQS:
        with open(os.path.join(ROOT, r), encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#")[0].strip()
                if not line or line.startswith("-"):
                    continue
                out.add(_norm(re.split(r"[<>=!\[;]", line)[0].strip()))
    return out


def _module_level_imports():
    """Direct children of the Module node — nothing inside a function, a class or a try/except."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "app")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            with open(path, encoding="utf-8") as fh:
                try:
                    tree = ast.parse(fh.read())
                except SyntaxError:
                    continue
            rel = os.path.relpath(path, ROOT)
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for a in node.names:
                        found.setdefault(a.name.split(".")[0], set()).add(rel)
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    found.setdefault(node.module.split(".")[0], set()).add(rel)
    return found


class RequirementsCoverTheImports(unittest.TestCase):
    def test_no_module_level_import_is_undeclared(self):
        declared = _declared()
        found = _module_level_imports()
        third_party = {n: f for n, f in found.items()
                       if n not in sys.stdlib_module_names
                       and n not in FIRST_PARTY and not n.startswith("_")}
        self.assertGreater(len(third_party), 10,
                           "almost nothing was collected — the walk or the parse is broken, and a "
                           "check that inspects nothing passes for ever")
        missing = {n: sorted(files) for n, files in third_party.items()
                   if _norm(DIST.get(n, n)) not in declared and _norm(n) not in declared}
        self.assertEqual(missing, {},
                         "these run at import time and are in neither requirements file, so a node "
                         "that deploys this code fails to start: "
                         + "; ".join(f"{n} ({', '.join(f)})" for n, f in missing.items()))

    def test_the_mapping_only_names_packages_that_are_actually_declared(self):
        """A stale entry in DIST silently satisfies an import whose package was removed."""
        declared = _declared()
        stale = sorted(mod for mod, dist in DIST.items() if _norm(dist) not in declared)
        # Not every alias is in use on every branch; what must not happen is an alias pointing at a
        # distribution nothing installs while its module is imported at module level.
        used = _module_level_imports()
        broken = [m for m in stale if m in used]
        self.assertEqual(broken, [],
                         f"these map to a distribution that is in no requirements file: {broken}")

    def test_both_requirements_files_parse(self):
        for r in REQS:
            self.assertTrue(_declared(), f"{r} produced no requirements")


if __name__ == "__main__":
    unittest.main()
