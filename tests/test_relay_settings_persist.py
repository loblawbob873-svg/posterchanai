"""The relay list and its scope PERSIST and HYDRATE -- in the app that saves them and in the worker
that acts on them.

The worker is a separate process: it hydrates settings at startup and re-hydrates from the relay every
two minutes (app/worker.py), and `relays.reconcile()` reads the list at call time on every delivery
tick. So a relay added in Admin is followed within ~2.5 minutes with no restart -- provided the keys
travel through the relay like every other admin setting. A key read as local-only never leaves the
process that wrote it, and the worker would never see the list at all.
"""
import re
from pathlib import Path

from app import schemas
from app.services import settings_store

ROOT = Path(__file__).resolve().parents[1]
KEYS = ("activitypub_relays", "activitypub_relay_scope")


def test_the_relay_settings_are_shared_so_every_process_hydrates_them():
    for key in KEYS:
        assert not settings_store._is_local_only(key), f"{key} would never reach the worker"


def test_the_relay_settings_are_declared_with_their_defaults():
    fields = schemas.SettingsResponse.model_fields
    assert fields["activitypub_relays"].default == ""
    assert fields["activitypub_relay_scope"].default == "local"


def test_the_scope_select_offers_exactly_what_the_code_accepts():
    """A hydrated value with no matching <option> shows the first option instead -- and the next Save
    posts THAT, silently changing the scope. The template and relays.scope() must agree."""
    html = (ROOT / "templates/admin/tabs/social.html").read_text()
    block = html[html.index('id="activitypub_relay_scope"'):]
    block = block[:block.index("</select>")]
    options = re.findall(r'<option value="([^"]+)"', block)
    assert options == ["local", "everyone"]
    from app.services.activitypub import relays
    for value in options:
        settings_store._CACHE["activitypub_relay_scope"] = value
        try:
            assert relays.scope() == value
        finally:
            settings_store._CACHE.pop("activitypub_relay_scope", None)
