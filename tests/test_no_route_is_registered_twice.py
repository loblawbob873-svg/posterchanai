"""No method + path is registered by two handlers.

Found in the 2026-10-08 cleanup (pyflakes: "redefinition of unused list_files"). FastAPI answers a request with the
FIRST handler registered for its method and path, so a second registration is code that can never run while
looking exactly like the one that does: storage.py had two `GET /api/storage/list-files` (the later one, with a
`depth` parameter, was dead), and storage.files_router re-registered `GET /api/files/search`, already served by
files.router. Both are removed; this walks every router the way main.py mounts it -- the router's own prefix plus
any prefix main.py adds at include_router -- and fails on the next duplicate.
"""
import ast
import importlib
import pkgutil
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter

import app.routers as R

MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def _include_prefixes():
    """{(module, attr): [prefixes main.py mounts it under]} from main.py's include_router calls."""
    out = defaultdict(list)
    for node in ast.walk(ast.parse(MAIN.read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "include_router" and node.args:
            a = node.args[0]
            if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name):
                pre = next((k.value for k in node.keywords if k.arg == "prefix"), None)
                out[(a.value.id, a.attr)].append(pre.value if isinstance(pre, ast.Constant) else ("<dynamic>" if pre else ""))
    return out


def test_every_method_and_path_has_one_handler():
    mounts = _include_prefixes()
    seen = defaultdict(list)
    done = set()
    for m in pkgutil.walk_packages(R.__path__, R.__name__ + "."):
        mod = importlib.import_module(m.name)
        short = m.name.rsplit(".", 1)[-1]
        for attr, obj in vars(mod).items():
            if not isinstance(obj, APIRouter) or id(obj) in done:
                continue
            done.add(id(obj))
            for pre in mounts.get((short, attr), [""]):
                for r in obj.routes:
                    for meth in getattr(r, "methods", None) or {"WS"}:
                        seen[(meth, pre + r.path)].append(f"{m.name}.{getattr(r.endpoint, '__name__', '?')}")
    dups = {k: v for k, v in seen.items() if len(v) > 1 and k[0] != "HEAD"}
    assert not dups, dups
