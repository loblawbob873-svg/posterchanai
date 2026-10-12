"""The local relay runs on the OS's own Python: numpy, websockets, httpx and the standard library ONLY.

app-misc/posterchan-relay depends on exactly those (dev-python/numpy, websockets, httpx) and NOT on the
server's venv, which only exists on a machine where somebody enabled "PosterChan Server". One stray import
-- a settings store, SQLAlchemy, the WoT crawl in thread.py, the package __init__ that used to import it
eagerly -- and the relay exits at start on every desktop, which the desktop then survives silently on
IndexedDB. So this imports the --os-mode entry in a subprocess whose import system REFUSES every top-level
module outside that set, and also checks the server-only modules never load.

Also: the negentropy INITIATOR the sync uses reconciles against the relay side (the server's own
`negentropy.reconcile`) to exactly the right need/have sets, over random sets.
"""
import os
import random
import subprocess
import sys
from pathlib import Path

from app.services.nostr_relay import negentropy

ROOT = Path(__file__).resolve().parents[1]

GUARD = r'''
import sys, importlib.abc
ALLOWED = {"numpy", "websockets", "httpx", "httpcore", "h11", "anyio", "sniffio", "idna", "certifi",
           "app", "_cffi_backend", "coincurve", "cffi"}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        top = name.split(".")[0]
        if top in sys.stdlib_module_names or top in ALLOWED or top.startswith("_"):
            return None
        raise ImportError("os-mode imported a module the OS does not ship: " + name)
sys.meta_path.insert(0, Guard())
sys.path.insert(0, ".")
from app.services.nostr_relay import osmode  # noqa
heavy = sorted(m for m in sys.modules if m in (
    "app.services.nostr_relay.thread", "app.services.settings_store", "app.database",
    "app.services.nostr_relay.firehose", "app.services.nostr_relay.outbox", "app.services.activitypub",
    "app.services.paid_retention_service"))
print("HEAVY", heavy)
'''


def test_os_mode_imports_nothing_the_os_does_not_ship():
    out = subprocess.run([sys.executable, "-c", GUARD], cwd=str(ROOT), capture_output=True, text=True,
                         timeout=120, env={**os.environ, "PYTHONPATH": ""})
    assert out.returncode == 0, out.stderr[-3000:]
    assert "HEAVY []" in out.stdout, ("os-mode pulled in server-only modules", out.stdout)


def test_relay_main_dispatches_os_mode_before_importing_the_server():
    src = (ROOT / "relay_main.py").read_text()
    assert src.index('"--os-mode"') < src.index("from app.services.nostr_relay import thread"), \
        "relay_main imports the whole server before it looks for --os-mode"


def test_the_initiator_reconciles_with_the_relay_side():
    rng = random.Random(7)
    for trial in range(25):
        ids = lambda k: [(rng.randint(1, 10 ** 6), os.urandom(32)) for _ in range(k)]  # noqa: E731
        common, mine, theirs = ids(rng.randint(0, 2500)), ids(rng.randint(0, 200)), ids(rng.randint(0, 200))
        server = sorted(common + theirs)
        ini = negentropy.Initiator(common + mine)
        msg, rounds = ini.initiate(), 0
        while msg is not None:
            msg = ini.reconcile(negentropy.reconcile(server, msg))
            rounds += 1
            assert rounds < 60, "negentropy did not converge"
        assert ini.need == {i for _, i in theirs}, trial
        assert ini.have == {i for _, i in mine}, trial
