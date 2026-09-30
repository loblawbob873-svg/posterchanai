"""A module the deploy table does not name restarts the units that can IMPORT it -- not all eight.

"Unmapped => restart everything" meant nearly every deploy restarted the relay (~30s of every Nostr
client disconnected), and during one of those windows the APK's calendar showed 0 events ("we cant
have our core apps not working if network outage"). The owners are measured from each unit's entry
point instead: every import (module level, inside functions, relative) and every string literal
naming an `app.` module (importlib tables), so the answer is a SUPERSET of what a process loads.
A module no unit can reach still restarts everything.
"""
import os
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import deploy_targets as dt  # noqa: E402


def _fake_repo(tmp: Path):
    files = {
        "run.py": "import app.role\n",
        "app/__init__.py": "", "app/role.py": "", "app/services/__init__.py": "",
        "app/main.py": "from app.services import only_app\n",
        "app/services/only_app.py": "def f():\n    from . import lazy_helper\n",   # relative + in-function
        "app/services/lazy_helper.py": "",
        "relay_main.py": "import app.services.relay_bits\n",
        "app/services/relay_bits.py": "",
        "app/worker.py": "JOBS = [('app.services.by_string', 'start')]\n",       # importlib table
        "app/services/by_string.py": "",
        "app/services/orphan.py": "",
    }
    for rel, body in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def test_owners_follow_lazy_relative_and_string_imports(tmp_path):
    _fake_repo(tmp_path)
    with mock.patch.object(dt, "_REPO", str(tmp_path)), mock.patch.object(dt, "_closures", None):
        assert dt.units_for(["app/services/only_app.py"]) == [dt.APP]
        assert dt.units_for(["app/services/lazy_helper.py"]) == [dt.APP], "an import inside a function counts"
        assert dt.units_for(["app/services/by_string.py"]) == [dt.WORKER], "a module named in a string table counts"
        assert dt.RELAY in dt.units_for(["app/services/relay_bits.py"])
        assert sorted(dt.units_for(["app/services/orphan.py"])) == sorted(dt.ALL), "unreachable is still everything"
        assert sorted(dt.units_for(["app/role.py"])) == sorted(dt.ALL), "run.py roots every unit"


def test_todays_app_only_changes_spare_the_relay():
    """Window AI and the calendar outage fix each restarted all eight units, relay included."""
    for p in ("app/services/chat_assist_service.py", "app/services/texts_ai_service.py"):
        got = dt.units_for([p])
        assert dt.APP in got and dt.RELAY not in got, (p, got)


def test_a_module_the_relay_really_imports_still_restarts_it():
    """The measurement must never under-restart: the relay's own store is in its closure."""
    assert dt.RELAY in dt.measured_owners("app/services/nostr_relay/store.py")
    assert dt.RELAY in dt.measured_owners("app/services/settings_store.py")
