"""The app's FastAPI(...) call passes only parameters FastAPI actually has.

2026-10-18: the ISO's server-install gate found every fresh install crash-looping --
`TypeError: FastAPI.__init__() got an unexpected keyword argument 'json_encoder'`. That keyword was never a FastAPI
parameter; releases up to 0.144 swallowed unknown keywords in `**extra` and ignored them, 0.145 (released that day)
removed `**extra`, and requirements allow `fastapi>=0.141.1,<1.0.0`, so a fresh pip resolve got it. server1's
venv (0.141.1) still started, which is why nothing here noticed. Checked against the NAMED parameters, so it fails
whatever FastAPI version this venv has.
"""
import ast
import inspect
from pathlib import Path

from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]


def test_every_keyword_passed_to_fastapi_is_a_named_parameter():
    named = {n for n, p in inspect.signature(FastAPI.__init__).parameters.items()
             if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}
    tree = ast.parse((ROOT / "app/main.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "FastAPI"]
    assert calls, "app/main.py no longer builds the app with FastAPI(...)"
    bad = [k.arg for c in calls for k in c.keywords if k.arg and k.arg not in named]
    assert not bad, "FastAPI() given keywords it does not define (a TypeError on fastapi>=0.145): %s" % bad
