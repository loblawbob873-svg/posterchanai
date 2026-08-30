"""The worker's schedulers must exist, and must start AFTER the settings hydrate.

Run: venv-unified/bin/python -m pytest tests/test_worker_schedulers_are_wired.py

`app/worker.py` is a separate process with its own `settings_store` cache, and the relay is the
authoritative store. It therefore has to hydrate settings BEFORE starting the schedulers — a
setting-gated scheduler that starts first reads its BUILD-TIME DEFAULT, decides it is switched off,
and never runs. `logs_scheduler_enabled` defaults to "false" and is "true" in the relay, so getting
this backwards means the scheduled health report simply never fires: no error, no warning, an
`active (running)` unit, and a report nobody notices the absence of until somebody asks why they
have not had one in a fortnight. CLAUDE.md lists it as the worker gotcha; nothing checked it.

The other half is cheaper and just as silent. `_SCHEDULERS` is a list of `(label, module, function)`
STRINGS, resolved with `importlib` inside a `try` that logs and moves on. Rename or move a start
function and that service is quietly dropped from the worker while every other one keeps running —
the process still starts, still logs "started N scheduler(s)", and N is simply one smaller than it
was.

Checked over the AST rather than by importing: the point is to run on any node, in any state, with
no database and no relay, so that a wiring mistake fails here rather than on a node at 3am.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "app", "worker.py")


def _tree(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read())


def _schedulers():
    """The (label, module, function) triples out of `_SCHEDULERS`."""
    for node in ast.walk(_tree(WORKER)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_SCHEDULERS":
                    out = []
                    for el in node.value.elts:
                        vals = [v.value for v in el.elts if isinstance(v, ast.Constant)]
                        if len(vals) == 3:
                            out.append(tuple(vals))
                    return out
    raise AssertionError("_SCHEDULERS is gone from app/worker.py")


def _module_path(dotted):
    return os.path.join(ROOT, *dotted.split(".")) + ".py"


class WorkerSchedulersAreWired(unittest.TestCase):

    def test_the_list_is_still_there_and_populated(self):
        """A parse that quietly returns [] would make every assertion below vacuous."""
        s = _schedulers()
        self.assertGreaterEqual(len(s), 8,
                                "only %d scheduler(s) parsed out of _SCHEDULERS — the shape of the "
                                "list has changed and this test has stopped reading it" % len(s))
        labels = [x[0] for x in s]
        self.assertEqual(len(labels), len(set(labels)), "duplicate scheduler labels: %s" % labels)

    def test_every_scheduler_module_exists(self):
        missing = [(lbl, mod) for lbl, mod, _fn in _schedulers()
                   if not os.path.exists(_module_path(mod))]
        self.assertEqual([], missing,
                         "worker schedulers naming modules that are not there (importlib fails "
                         "inside a try/except, so the service is silently dropped): %s" % missing)

    def test_every_start_function_exists_in_its_module(self):
        missing = []
        for lbl, mod, fn in _schedulers():
            p = _module_path(mod)
            if not os.path.exists(p):
                continue
            names = {n.name for n in ast.walk(_tree(p))
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            if fn not in names:
                missing.append((lbl, mod, fn))
        self.assertEqual([], missing,
                         "worker schedulers whose start function no longer exists — the worker "
                         "logs one line and carries on without them: %s" % missing)

    def test_settings_are_hydrated_before_any_scheduler_starts(self):
        """The ordering IS the feature. Start first and every gated scheduler reads its default."""
        fn = None
        for node in ast.walk(_tree(WORKER)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_run":
                fn = node
        self.assertIsNotNone(fn, "app/worker.py no longer defines _run()")

        hydrate_line = start_line = None
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if name == "hydrate_from_db" and hydrate_line is None:
                    hydrate_line = node.lineno
            # The scheduler loop: `for name, module, fn in _SCHEDULERS:`
            if isinstance(node, ast.For) and isinstance(node.iter, ast.Name) \
                    and node.iter.id == "_SCHEDULERS" and start_line is None:
                start_line = node.lineno

        self.assertIsNotNone(hydrate_line,
                             "_run() no longer calls settings_store.hydrate_from_db() — every "
                             "setting-gated scheduler in this process now reads its build-time "
                             "default and silently never runs")
        self.assertIsNotNone(start_line, "_run() no longer iterates _SCHEDULERS")
        self.assertLess(hydrate_line, start_line,
                        "the schedulers start at line %s, BEFORE the settings hydrate at line %s: "
                        "each gated one reads its build-time default and never runs, with nothing "
                        "in any log to say so" % (start_line, hydrate_line))

    def test_local_settings_are_loaded_before_the_relay_hydrate(self):
        """`load_local()` carries the per-node cursors (fedi_bridge_global_since and friends). The
        relay hydrate skips those keys, so loading it second — or not at all — starts every poller
        with no cursor on every restart: 'cursor lost — resuming…', and work redone each boot."""
        src = open(WORKER, encoding="utf-8").read()
        self.assertIn("load_local()", src,
                      "_run() no longer loads local settings — the per-node poller cursors are "
                      "lost on every worker restart")
        self.assertLess(src.index("load_local()"), src.index("hydrate_from_db"),
                        "load_local() must run before the relay hydrate")


if __name__ == "__main__":
    unittest.main()
