"""THE VAULT KEY IS NOT ALWAYS A COMMUNITY ID, AND WRITING IT INTO THE BUNDLE BREAKS THE ROOM.

Reported from the web UI, live:

    Concord metadata sync failed  Error: invalid Concord join material
      runtime .../cord-reader.js  control  inspectControl  refreshRoomMetadata

A room joined by a plain invite link has no `community_id`, so its membership-vault entry is keyed
on `roomIdentity(room)` -- the naddr -- which is what lets it reach the account's other devices at
all. But `decodeMembershipLists` handed that key to `cordListMaterial`, which injects whatever it is
given as the material's `community_id`. On a device rebuilding the room FROM the vault, the real
32-byte id inside the join bundle was therefore replaced by a naddr, `inspectControl` rejected it,
and every read of the room threw.

IT IS INVISIBLE FROM THE DEVICE THAT PUBLISHED IT. There the local row is kept as-is
(`if(i>=0&&rooms[i].cord&&!rooms[i].cord.armadaList)continue`), so the room works perfectly on the
machine the change was measured on and is broken on every other one -- which is exactly the device
the change exists to serve. That is why this is driven as a SIMULATION of the second device rather
than asserted against the source text: a string match would have passed while the room was broken.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_a_bundle_keeps_its_own_community_id():
    run = subprocess.run(["node", str(ROOT / "tests/client/concord_vault_key_sim.mjs")],
                         cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert "vault key simulation: ok" in run.stdout


def test_removal_matches_the_way_the_tombstone_was_keyed():
    """A leave writes its tombstone under `roomIdentity`; removal must look there too.

    Keyed only on `room.communityId`, a leave never fires for an invite-link room -- the room this
    identity change was made for. Leave it on the phone and the laptop shows it for ever, while the
    backfill correctly declines to re-add it, so nothing reconciles.
    """
    src = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
    at = src.index("kept=rooms.filter(")
    line = src[at: src.index("\n", at)]
    assert "dead.has(roomIdentity(room))" in line, line
    assert "dead.has(room.communityId)" in line, (
        "the older tombstone form names only the community id and must still match")
