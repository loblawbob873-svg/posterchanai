"""The relay carries photo albums: NIP-68 pictures (kind 20) and NIP-51 picture sets (kind 30006).

Profile → Albums reads both. Neither was in the relay's default `ingest_kinds`, so an album made in
another client (Olas and friends) never reached this relay -- only the ones made here were shown. And
an album is somebody's collection: neither kind may be aged out by the pruner or refused as retired.
"""
import re
from pathlib import Path

from app.services.nostr_relay import store

ROOT = Path(__file__).resolve().parents[1]


def _default_ingest_kinds() -> set:
    src = (ROOT / "app/services/nostr_relay/thread.py").read_text()
    m = re.search(r'g\("nostr_relay_ingest_kinds",\s*"([0-9,]+)"\)', src)
    assert m, "could not find the nostr_relay_ingest_kinds default in thread.py"
    return {int(k) for k in m.group(1).split(",")}


def test_the_relay_pulls_pictures_and_picture_sets_from_upstream():
    kinds = _default_ingest_kinds()
    assert 20 in kinds, "NIP-68 picture posts are never synced -- another client's photos never arrive"
    assert 30006 in kinds, "NIP-51 picture sets are never synced -- another client's albums never arrive"


def test_an_album_and_its_photos_are_never_pruned_or_refused():
    for kind in (20, 30006):
        assert kind not in store._PRUNABLE_KINDS, f"kind {kind} would be aged out of somebody's album"
        assert kind not in store._RETIRED_KINDS, f"kind {kind} would be refused and deleted as retired"
