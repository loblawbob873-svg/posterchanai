"""THE LOCAL SETTINGS FILE CARRIES A DATABASE PASSWORD, AND IT WAS WORLD-READABLE.

`/var/lib/posterchanai/local_settings.json` holds the plumbing keys the relay needs before anything
is hydrated — including `nostr_relay_pg_dsn`, which on this deployment is:

    host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan password=<REDACTED>

`json.dump` into a file opened with a default mode leaves it 0644 under a normal umask. MEASURED on
this node: `-rw-r--r--`, on a host that also has the `pleroma`, `linkbot`, `misskey` and `aikey`
accounts. Any of them could read those credentials and connect to the store that holds every user's
events, encrypted DMs, notes, calendars and contacts.

It is not enough to chmod the file: the app rewrites it whenever a local key changes (bridge
cursors, autopost stamps), and a chmod applied by hand was measured back at 0644 within seconds. The
MODE HAS TO COME FROM THE WRITER, on the temp file, before any content reaches it — so the published
file is never briefly readable — and on every write, so a file that already exists with loose
permissions is tightened rather than preserved.
"""
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from app.services import settings_store


def test_the_writer_creates_the_file_private(tmp_path, monkeypatch):
    target = tmp_path / "local_settings.json"
    monkeypatch.setattr(settings_store, "_LOCAL_PATH", str(target))
    with settings_store._lock:
        settings_store._CACHE["nostr_relay_pg_dsn"] = "host=x password=hunter2"
        settings_store._LOCAL_DIRTY.add("nostr_relay_pg_dsn")
    settings_store._save_local_file()
    assert target.is_file(), "the writer produced no file"
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"local settings written {oct(mode)} — group/other can read the DB password"


def test_a_file_that_already_exists_loose_is_tightened(tmp_path, monkeypatch):
    """The failure mode this actually had: the file was already 0644 from an older build, and every
    subsequent write preserved that."""
    target = tmp_path / "local_settings.json"
    target.write_text(json.dumps({"nostr_relay_pg_dsn": "old"}))
    os.chmod(target, 0o644)
    monkeypatch.setattr(settings_store, "_LOCAL_PATH", str(target))
    with settings_store._lock:
        settings_store._CACHE["nostr_relay_port"] = "3052"
        settings_store._LOCAL_DIRTY.add("nostr_relay_port")
    settings_store._save_local_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600, "a pre-existing 0644 file stayed readable"


def test_the_mode_is_set_before_any_content_is_written():
    """A file created 0644 and chmodded afterwards is briefly readable by anyone who is looking.
    The temp file is opened with the mode, and os.replace carries it across."""
    import inspect
    src = inspect.getsource(settings_store._save_local_file)
    opened = src.index("os.open(tmp")
    dumped = src.index("json.dump(merged")
    assert opened < dumped, "content is written before the mode is set"
    assert "0o600" in src

# The LIVE node's own file is checked by scripts/check_secrets_not_world_readable.py, not here:
# tests/conftest.py redirects `_LOCAL_PATH` to a tmp dir for every test (rightly — a test must not
# touch this host), so a pytest assertion about the real file can only ever skip. It did, on a node
# where the file was 0644 at that very moment.
