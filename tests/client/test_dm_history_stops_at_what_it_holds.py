"""A returning device stops its DM history drain at what it already holds (DmCache), instead of paging the whole
inbox through the relay on every visit -- 106 pages of 500 wraps per 5 minutes on server1, 2026-10-17. A device
with an empty cache still reads everything. See the .mjs; it RUNS the shipped ensureDMs."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_a_returning_device_reads_one_page_and_an_empty_cache_reads_all():
    result = subprocess.run(
        ["node", str(ROOT / "tests/client/dm_history_stops_at_what_it_holds_runtime.mjs")],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
