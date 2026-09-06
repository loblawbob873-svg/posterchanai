"""LEAVING A CONCORD COMMUNITY HAS TO STICK.

Reported: "make sure you can leave communities! on laptop, i loaded Communities and it brought me
back to Soapbox which I left many times", and earlier "mobile has no way to leave concord
communities. Concord communities avatar keeps disappearing and coming back".

Three separate faults, each measured before it was fixed:

1. THE VAULT TOMBSTONE COULD NOT BE MATCHED BY THE THING THAT RESURRECTS A ROOM. Leaving writes a
   kind-13302 tombstone keyed on `community_id`. The resurrection path is `recoverOwnedInvite`:
   discovery replays the OWNER'S OWN kind-1 invite announcement, which knows a naddr and a URL and
   no community id at all, and rebuilds the room from it. The only thing that ever refused was a
   localStorage ledger belonging to the device the Leave button was pressed on — so the laptop
   walked straight back in, and the rebuilt room (carrying no community id) then survived the
   tombstone filter too.

2. THE CONTROL WAS NOT ON THE SCREEN A PHONE OPENS ON — it lived only in `.cc-conversation`'s
   header, which is `display:none` on a phone until a channel is opened, and it was gated on
   `window.confirm`, which a WebView may suppress (answering false) and which opens a real OS
   window in the desktop shell.

3. THE AVATAR FLICKER is its own bug, in `applyRoomIconMetadata`: see
   `concord_icon_empty_read_runtime.mjs`.

The browser end of this is `scripts/check_concord_leave.py`, which drives the real client at phone
and desktop width.
"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/concord.js").read_text()


def _runtime(name, marker):
    run = subprocess.run(["node", str(ROOT / "tests/client" / name)], cwd=ROOT,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert marker in run.stdout


def test_leaving_survives_the_next_device():
    _runtime("concord_leave_durability_runtime.mjs", "concord leave durability runtime ok")


def test_an_empty_control_read_never_clears_a_community_icon():
    _runtime("concord_icon_empty_read_runtime.mjs", "concord icon empty-read runtime ok")


def test_the_tombstone_names_the_invite_it_retired():
    """A tombstone keyed only on `community_id` is unmatchable by an announcement, which is the
    only thing that can put the room back."""
    leave = SRC.split("async function leaveArmadaMembership", 1)[1].split("async function ", 1)[0]
    assert "invite_ref:leftRef" in leave and "naddr:leftNaddr" in leave, leave[:400]
    assert "entries.delete(cid)" in leave, (
        "leaving must drop the entry as well as add the tombstone; keeping it is correct only "
        "because removed_at happens to outrank added_at"
    )


def test_a_winning_tombstone_teaches_this_device_that_the_account_left():
    sync = SRC.split("async function syncArmadaMemberships", 1)[1].split("async function ", 1)[0]
    assert "for(const id of dead){" in sync
    assert "noteLeftFromVault(viewer.pubkey" in sync, (
        "nothing replays the vault's tombstones into the ledger the resurrection paths consult"
    )
    # ONLY the winning ones: a re-join publishes an entry newer than its tombstone, and re-seeding
    # from the stale tombstone beside it would refuse the join the person just made.
    assert sync.index("const dead=new Set(") < sync.index("noteLeftFromVault(viewer.pubkey")


def test_an_announcement_waits_for_the_vault_to_be_read():
    """Discovery answers in milliseconds and the membership read is several round trips behind it,
    so the owner's own kind-1 rebuilt the room before anything had learned it was tombstoned — and
    `recoveredOwnedInvites` then made that decision final for the session."""
    recover = SRC.split("function recoverOwnedInvite(p,item)", 1)[1].split("\n  function ", 1)[0]
    assert "membershipReadFor!==viewer.pubkey" in recover
    assert "pendingOwnedInvites.push(item)" in recover
    assert recover.index("membershipReadFor!==viewer.pubkey") < recover.index("recoveredOwnedInvites.add"), (
        "the session-final mark is taken before the vault has been read"
    )
    assert "if(viewer.pubkey&&(recovered||vaultEmpty)){membershipReadFor=viewer.pubkey;flushOwnedInvites(p);}" in SRC, (
        "'a document was opened' is the only honest latch — candidates that decoded nothing means "
        "'could not open', never 'there is nothing there'"
    )


def test_the_leave_control_is_on_the_screen_a_phone_opens_on():
    """`.cc-conversation` is display:none on a phone until a channel is opened, so a control that
    lives only in its header is present and 0x0 — measured, and reported as "mobile has no way to
    leave concord communities"."""
    assert 'id="cc-leave-room"' in SRC, "the rooms/channels pane has no leave control"
    channels = SRC.split('<aside class="cc-channels">', 1)[1].split("</header>", 1)[0]
    assert 'id="cc-leave-room"' in channels
    assert "const leaveRoom=$('#cc-leave-room');if(leaveRoom)leaveRoom.onclick=leaveByHeader;" in SRC
    assert "const leaveShortcut=$('#cc-leave-shortcut');if(leaveShortcut)leaveShortcut.onclick=leaveByHeader;" in SRC


def test_leaving_is_never_gated_on_a_native_dialog():
    """A suppressed WebView dialog answers false, and this confirm was the ONLY gate on Leave."""
    handler = SRC.split("const leave=$('#cc-leave-community')", 1)[1].split("const leaveByHeader", 1)[0]
    assert "window.confirm(" not in handler
    assert "await p.uiConfirm('Leave '" in handler
    assert handler.index("await leaveArmadaMembership(p,room)") < handler.index("const latest=saved()")
