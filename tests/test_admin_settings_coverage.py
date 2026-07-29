"""The admin form is generic: static/js/admin.js hydrates every input by `id` from
GET /api/admin/settings and posts back every input that has a `name`. GET returns a typed
SettingsResponse, so a field the schema doesn't declare is DROPPED from the response — the input
then loads blank/unchecked on every visit no matter what is stored in the relay.

For a text field that only looks broken. For a CHECKBOX it is destructive: an unhydrated box posts
"false" on the next Save, silently turning the feature off (this is exactly what happened to
`telegram_local_api` and `llm_flash_attn`, which were read at runtime but never declared here).

So: every named input in the admin tabs must exist in SettingsResponse, and its `id` must equal its
`name` or hydration writes to a different element than the save reads.
"""
import re
from pathlib import Path

import pytest

from app.schemas import SettingsResponse

TABS = Path(__file__).resolve().parents[1] / "templates" / "admin" / "tabs"

# Fields that are deliberately NOT settings: they belong to their own API and must never be posted
# to /api/admin/settings. They carry no `name` attribute, so they're excluded by construction —
# this list only documents the pattern for the next person adding one.
_NOT_SETTINGS = ("emoji manager controls", "external storage modal", "test_email_address")


def _named_inputs():
    """[(key, id, file)] for every settings-form control that has a name."""
    out = []
    for path in sorted(TABS.glob("*.html")):
        html = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", html):
            name = re.search(r'\bname="([^"]+)"', tag)
            if not name:
                continue
            el_id = re.search(r'\bid="([^"]+)"', tag)
            out.append((name.group(1), el_id.group(1) if el_id else None, path.name))
    return out


def test_every_named_admin_input_is_a_declared_setting():
    fields = set(SettingsResponse.model_fields)
    missing = sorted({(k, f) for k, _i, f in _named_inputs() if k not in fields})
    assert not missing, (
        "these admin inputs post a setting the schema doesn't declare, so GET /settings drops it "
        "and the field never hydrates (checkboxes then save 'false' over the stored value): "
        + ", ".join(f"{k} ({f})" for k, f in missing)
    )


def test_named_admin_inputs_have_a_matching_id():
    bad = [(k, i, f) for k, i, f in _named_inputs() if i != k]
    assert not bad, (
        "admin.js hydrates by id and saves by name — these differ, so the loaded value and the "
        "saved value are two different elements: " + ", ".join(f"{f}: id={i!r} name={k!r}" for k, i, f in bad)
    )


@pytest.mark.parametrize("tab", sorted(p.name for p in TABS.glob("*.html")))
def test_every_tab_pane_is_reachable_from_the_nav(tab):
    """Each tab file declares one #tab-<name> pane, and admin.html must have a button for it —
    an orphaned pane is invisible, and an orphaned button throws on click."""
    admin_html = (TABS.parents[1] / "admin.html").read_text(encoding="utf-8")
    panes = re.findall(r'<div class="tab-content[^"]*" id="tab-([a-z0-9_-]+)"', (TABS / tab).read_text(encoding="utf-8"))
    assert panes, f"{tab} declares no .tab-content pane"
    for pane in panes:
        assert f'data-tab="{pane}"' in admin_html, f"{tab}: no nav button for tab '{pane}'"
        assert f"admin/tabs/{tab}" in admin_html, f"{tab} is not included by admin.html"
