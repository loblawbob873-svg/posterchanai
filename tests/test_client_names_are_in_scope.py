"""Every name a client module reads is bound in an ENCLOSING scope — not merely declared somewhere in the file.

test_client_module_deps.py checks names file-wide, so a name declared in ONE function counts as known in every
other: Concord's Invite button passed `current`, which only existed inside the header renderer, and every press
threw "current is not defined". The same blind spot hid two more: the Joplin import declared its counters inside
a `try` and read them after `finally` (every import ended in "reused is not defined" instead of its summary), and
Files → Drive check wrote into `r`, a dialog callback's argument, whenever a folder sync was running.

This runs a scope-aware walk (tests/client/unbound_names.cjs, acorn) over EVERY client script and fails on any
read whose name is declared elsewhere in the file but not in scope where it is read. Browser globals (`screen`,
`location`…) that happen to share a local's name elsewhere are fine: an unbound read reaches `window`.
"""
import glob
import json
import os
import subprocess

import pytest

from tests import test_client_module_deps as deps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = sorted(glob.glob(os.path.join(ROOT, "static", "js", "client", "*.js")))
BROWSER = set(deps.GLOBALS) | {"screen", "name", "status", "event", "origin", "parent", "top", "length", "frames",
                                "opener", "closed", "self", "globalThis", "window", "document"}


@pytest.mark.skipif(not getattr(deps, "ACORN", None), reason="acorn is not installed")
@pytest.mark.parametrize("path", FILES, ids=lambda p: os.path.basename(p))
def test_every_name_read_is_in_scope(path):
    r = subprocess.run(["node", os.path.join(ROOT, "tests", "client", "unbound_names.cjs"), path],
                       capture_output=True, text=True, timeout=120, env=dict(os.environ, PC_ACORN=deps.ACORN))
    if r.returncode != 0:
        pytest.skip("not parseable as a script: %s" % r.stderr.strip().splitlines()[-1:])
    bad = [x for x in json.loads(r.stdout) if x["name"] not in BROWSER]
    assert not bad, ("%s reads names that are declared elsewhere in the file but NOT in scope where they are "
                     "read — each throws ReferenceError when that line runs:\n  %s"
                     % (os.path.basename(path), "\n  ".join(sorted({"%s:%d %s" % (os.path.basename(path), x["line"],
                                                                     x["name"]) for x in bad}))))
