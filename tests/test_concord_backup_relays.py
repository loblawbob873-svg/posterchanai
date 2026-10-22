"""The backups a hosted community is copied to are the backups its invite lists (2026-10-21).

The server copies a community hosted here to concord_mirror.BACKUP_RELAYS; the client lists the same relays in the
community's relay list (concord.js CORD_BACKUP_RELAYS) so any app can find the copies. Two lists that drift would
copy to one place and point readers at another. The protocol half is cord_metadata_relays_runtime.mjs.
"""
import json
import re
import subprocess
from pathlib import Path

from app.services.nostr_relay import concord_mirror as cm

ROOT = Path(__file__).resolve().parents[1]
CONCORD = (ROOT / "static/js/client/concord.js").read_text()


def test_the_server_and_the_client_name_the_same_backups():
    m = re.search(r"const CORD_BACKUP_RELAYS=(\[[^\]]*\]);", CONCORD)
    assert m, "CORD_BACKUP_RELAYS is gone from concord.js"
    assert json.loads(m.group(1).replace("'", '"')) == list(cm.BACKUP_RELAYS)


def test_the_relay_list_fits_the_protocol_cap():
    # this node + the backups must fit CORD's MAX_COMMUNITY_RELAYS (5), or a reader silently drops the tail
    assert 1 + len(cm.BACKUP_RELAYS) <= 5


def test_only_someone_who_may_edit_the_community_writes_its_relays():
    body = CONCORD[CONCORD.index("async function listBackups(p,room,bundle,wraps){"):][:1400]
    assert "if(owner!==me&&!memberMay(room,me,'MANAGE_METADATA'))return false;" in body
    assert "backupListTried.has(roomIdentity(room))" in body and "backupListTried.add(roomIdentity(room))" in body
    assert "saveCommunitySettings(p,room,{relays})" in body


def test_the_protocol_writer_and_reader_agree():
    r = subprocess.run(["node", "tests/client/cord_metadata_relays_runtime.mjs"], cwd=ROOT, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr
