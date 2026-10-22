"""Every screen of the client has a BROWSER test that drives it, or is listed as a known gap with a reason
(2026-10-22: "every feature needs test coverage to prevent stupid experiences for the user").

scripts/feature_coverage.py discovers the screens (every data-view in the client shell + the ones opened another
way) and the browser tests that reach each one. The smoke check (check_every_view_opens.py) is the floor -- a screen
that DRAWS -- and is not counted. AI screens are out of scope for now.

The list only shrinks: a NEW screen with no browser test fails here (write the test, or list it with a reason), and a
listed gap that has since gained a test fails too (take it off, so the list stays the truth)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fc", ROOT / "scripts" / "feature_coverage.py")
fc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fc)

COV = fc.coverage()


def test_the_inventory_is_discovered_not_typed():
    assert len(COV) >= 40, f"only {len(COV)} screens found -- has the client shell moved?"
    assert "concord" in COV and "settings" in COV and "profile" in COV


def test_every_screen_has_a_browser_test_or_a_stated_reason():
    missing = sorted(v for v, tests in COV.items() if not tests and v not in fc.KNOWN_GAPS)
    assert not missing, ("these screens have no browser test and are not on KNOWN_GAPS: " + ", ".join(missing)
                         + ". Write a test that drives the screen (switchView + its main control), or list it in "
                           "scripts/feature_coverage.py with a reason.")


def test_the_gap_list_only_shrinks():
    covered = sorted(v for v in fc.KNOWN_GAPS if COV.get(v))
    assert not covered, "now covered -- take them off KNOWN_GAPS: " + ", ".join(covered)
    unknown = sorted(v for v in fc.KNOWN_GAPS if v not in COV)
    assert not unknown, "KNOWN_GAPS names screens that no longer exist: " + ", ".join(unknown)
    assert all(str(why).strip() for why in fc.KNOWN_GAPS.values()), "every gap needs a reason"


def test_ai_screens_are_the_only_exclusion():
    assert fc.AI_VIEWS == {"ai", "translate"}
