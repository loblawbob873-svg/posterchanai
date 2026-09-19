"""The Admin -> VMs tab is a clean, card-based panel with a color-coded status BOARD, not a raw
<pre> text dump — while keeping every hydrating input (test_vmhost_admin_settings.py owns that).

Reported twice as needing to "look nicer and cleaner". The status readout used to be a
`<pre id="vmhost-status-result">` that printed newline-joined strings; it is now a grid of rows,
each a colored dot + label + value, and the master enable switch is lifted into its own card.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAB = (ROOT / "templates/admin/tabs/vmhost.html").read_text(encoding="utf-8")


def test_the_raw_pre_status_dump_is_gone():
    assert 'id="vmhost-status-result"' not in TAB, "the <pre> text dump should be replaced by a board"
    assert "<pre" not in TAB, "no raw pre readout in the redesigned tab"


def test_there_is_a_colour_coded_status_board():
    assert 'id="vmhost-status-board"' in TAB, "the status board container is missing"
    # a row builder with a state dot + label + value
    assert "vmh-srow" in TAB and "vmh-sdot" in TAB and "vmh-slabel" in TAB and "vmh-sval" in TAB
    # the three health states each get a colour
    for cls in ("vmh-sdot.ok", "vmh-sdot.warn", "vmh-sdot.bad"):
        assert cls in TAB, f"missing status colour {cls}"
    # it renders the real fields the status endpoint returns
    for field in ("libvirt", "kvm", "storage", "node_npub"):
        assert field in TAB, f"the board should show {field}"


def test_the_master_switch_is_its_own_card_and_limits_are_a_grid():
    assert "vmh-master" in TAB and 'id="vmhost_enabled"' in TAB
    assert "vmh-grid" in TAB, "numeric limits should lay out in a responsive grid"


def test_every_setting_still_lives_inside_a_label_for_hydration():
    # admin.js hydrates/saves by id==name; each must still be a labelled control (the redesign is
    # layout-only). This mirrors the stricter check in test_vmhost_admin_settings.py as a tripwire.
    names = set(re.findall(r'\bname="(vmhost_[a-z_]+)"', TAB))
    assert len(names) >= 25, f"expected all vmhost_* inputs, found {len(names)}"
    for n in names:
        assert re.search(r'<label\b[^>]*>(?:(?!</label>).)*\bname="%s"' % re.escape(n), TAB, re.S), \
            f"{n} is not inside a <label> — it will not hydrate"
