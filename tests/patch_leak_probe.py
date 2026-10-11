"""A pytest plugin (`-p tests.patch_leak_probe`) that reports every `app.*` FUNCTION or CLASS a test run left replaced.

Snapshot after collection (every module the tests import is loaded by then), compare at the end of the session, and
write what differs to $PC_PATCH_LEAK_REPORT as JSON. Used by tests/test_patches_are_undone.py in a subprocess, so
the run it inspects shares nothing with the gate's own process.
"""
import inspect
import json
import os
import sys

_snap = {}


def _module_callables():
    out = {}
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name == "app" or name.startswith("app.")):
            continue
        for attr, value in list(vars(mod).items()):
            if inspect.isfunction(value) or inspect.isclass(value):
                out[(name, attr)] = value
    return out


def pytest_collection_finish(session):
    _snap.clear()
    _snap.update(_module_callables())


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("PC_PATCH_LEAK_REPORT")
    if not path:
        return
    leaked = []
    for (name, attr), original in _snap.items():
        mod = sys.modules.get(name)
        if mod is None:
            continue
        now = vars(mod).get(attr, original)
        if now is not original:
            leaked.append("%s.%s is now %r" % (name, attr, now))
    with open(path, "w") as f:
        json.dump(sorted(leaked), f)
