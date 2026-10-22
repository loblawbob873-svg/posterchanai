"""The number of client buttons that no test names may only go DOWN (2026-10-22: "every feature needs test coverage").

scripts/control_coverage.py collects every <button id> in static/js/client/*.js (AI excluded) and checks whether any
test names it. On 2026-10-22 that was 384 of 767. UNTESTED_CEILING is that number, lowered as tests land:
  - a NEW button with no test raises the count past the ceiling and fails here -- write its test;
  - a test that covers buttons lowers the count; lower the ceiling to match (the second test insists), so the
    ground gained can never be quietly given back.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("cc", ROOT / "scripts" / "control_coverage.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

UNTESTED_CEILING = 284

UNTESTED = {i for ids in cc.untested().values() for i in ids}


def test_no_new_button_ships_without_a_test():
    assert len(UNTESTED) <= UNTESTED_CEILING, (
        f"{len(UNTESTED) - UNTESTED_CEILING} more untested button(s) than before. A new button needs a test that "
        f"presses it. Run: venv-unified/bin/python scripts/control_coverage.py")


def test_the_ceiling_follows_progress_down():
    assert len(UNTESTED) >= UNTESTED_CEILING, (
        f"untested buttons fell to {len(UNTESTED)} -- lower UNTESTED_CEILING to {len(UNTESTED)} so it cannot creep back")


def test_the_inventory_is_discovered():
    assert len(cc.buttons()) > 500, "found few buttons -- has the client moved?"
