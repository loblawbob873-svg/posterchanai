"""An installed-* gate must not describe a stale bundle as a defect in this working tree.

`check_installed_code_package_release` reported five problems against PosterChan Code — one of them
its discard dialog "not saying what it would lose", while quoting verbatim the sentence this tree had
already replaced. It was reading /opt/posterchan/resources/app.asar, built several commits earlier.
That failure mode is recorded in memory as `project_installed_asar_stale_false_red.md`, and it
happened AGAIN because the guard lived in one gate and not in its siblings.

So there is one copy of the question, and every gate that judges the installed bundle asks it.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "scripts/_installed_stamp.py"
# Gates that run the INSTALLED bundle and could therefore blame it on this tree.
GATES = ["check_installed_code_package_release.py", "check_installed_document_apps_release.py"]


def test_the_question_is_asked_in_one_place():
    assert SHARED.is_file()
    src = SHARED.read_text(encoding="utf-8")
    assert "def stale_reason(" in src and "def installed_stamp(" in src


def test_every_installed_bundle_gate_consults_it():
    for name in GATES:
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "from _installed_stamp import stale_reason" in src, name
        assert "stale_reason(asar" in src, name
        # And SKIPs (exit 2) rather than failing: nothing about this tree was verified.
        after = src.split("stale_reason(asar", 1)[1][:400]
        assert "return 2" in after, f"{name} must exit 2, not fail: {after[:200]}"


def test_a_second_hand_written_copy_cannot_come_back():
    """Two copies is how one of them keeps the guard and the other keeps the false red."""
    for name in GATES:
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "__PC_BUILD" not in src, f"{name} re-reads the stamp itself instead of sharing"


def test_an_unreadable_stamp_is_not_treated_as_stale():
    """With nothing to compare there is nothing to judge; refusing on that basis would make every
    bundle built before stamping a permanent skip."""
    src = SHARED.read_text(encoding="utf-8")
    body = src.split("def installed_stamp(", 1)[1]
    assert body.count('return ""') >= 3, body[:400]


def test_the_comparison_tolerates_a_different_abbreviation_length():
    src = SHARED.read_text(encoding="utf-8")
    assert re.search(r"stamp\.startswith\(at\)\s*or\s*at\.startswith\(stamp\)", src), src


def test_there_is_a_deliberate_way_to_gate_an_older_bundle():
    assert 'PC_ALLOW_STALE_INSTALL' in SHARED.read_text(encoding="utf-8")
