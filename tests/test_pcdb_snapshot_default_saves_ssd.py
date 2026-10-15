"""PosterChanDB snapshots the index once a DAY by default, not once an hour, everywhere the default is spelled.

Measured on server1 2026-10-15: the index snapshot is 1.24 GB and was rewritten every hour (`maintenance:
{'snapshot': True}` hourly; the relay process wrote 18.0 GB in 10.3 h, ~42 GB/day, to the SSD). A clean stop always
snapshots, so the periodic one only bounds how much log a CRASH replays -- ~13 s for a day of server1's traffic
(~20k events/s replay, ~260k events/day). The owner: "reducing writes is good".

The default lived in four places that had to agree (the store's own housekeeper, the admin tunable, the settings
schema, the admin placeholder) and drifted to 1 hour together; this pins all four.
"""
import re
from pathlib import Path

from app import schemas
from app.services import relay_tunables as rt

ROOT = Path(__file__).resolve().parents[1]


def test_the_admin_tunable_and_the_schema_default_to_a_day():
    assert rt.PCDB["posterchandb_snapshot_hours"].default == 24.0
    assert schemas.SettingsResponse.model_fields["posterchandb_snapshot_hours"].default == 24.0
    assert rt.resolve("posterchandb_snapshot_hours", "") == 24.0, "a blank stored value must mean a day"


def test_the_store_starts_its_housekeeper_at_the_same_default():
    src = (ROOT / "app/services/nostr_relay/pcdb_store.py").read_text()
    got = re.findall(r"_Housekeeper\([^)]*snapshot_hours=([0-9.]+)", src)
    assert got and all(float(v) == rt.PCDB["posterchandb_snapshot_hours"].default for v in got), got


def test_the_admin_field_shows_the_default():
    html = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text()
    m = re.search(r'name="posterchandb_snapshot_hours"[^>]*placeholder="([0-9.]+)"', html)
    assert m and float(m.group(1)) == 24.0, m and m.group(0)
