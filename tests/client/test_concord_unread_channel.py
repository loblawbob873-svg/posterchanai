"""A channel nobody could read must not announce itself as a channel with nothing in it.

Measured on two joined Vector rooms: their relay set includes `wss://asia.vectorapp.io/nostr`,
which answers every kind-1059 filter with `auth-required` and then refuses the AUTH itself
("relay needs serviceUrl to be configured before AUTH can work"). Its gift wraps are unreadable to
any client, and after the first failure `queryFrom` cools it down and silently returns `[]` — so
Concord drew "This is the start of this encrypted channel" over a conversation the same account
could read in another app.
"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _node(script):
    run = subprocess.run(["node", str(ROOT / "tests/client" / script)], cwd=ROOT,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stdout + run.stderr
    return run.stdout


def test_the_shipped_empty_state_distinguishes_unread_from_empty():
    assert "concord unread channel runtime ok" in _node("concord_unread_channel_runtime.mjs")


def test_query_from_reports_which_relays_answered_and_a_retry_lifts_the_cooldown():
    assert "relay query reach runtime ok" in _node("relay_query_reach_runtime.mjs")


def test_the_empty_state_is_the_only_place_that_sentence_is_written():
    """Two copies of the claim is how one of them keeps being right and the other keeps lying."""
    src = (ROOT / "static/js/client/concord.js").read_text()
    # Prose about the bug is not a second copy of the bug; count what the user can actually be shown.
    drawn = [line for line in src.splitlines()
             if "start of this encrypted channel." in line and "cc-welcome" in line]
    assert len(drawn) == 1, (
        "the empty-channel claim must be rendered in exactly one place, so it can be qualified in "
        f"one place; found {len(drawn)}"
    )


def test_the_reach_record_is_threaded_all_the_way_to_the_transport():
    """A report that never reaches `Relay.queryFrom` is a record of nothing, and the empty state
    that reads it would be confidently wrong in the other direction."""
    concord = (ROOT / "static/js/client/concord.js").read_text()
    relay = (ROOT / "static/js/client/relay.js").read_text()
    app = (ROOT / "static/js/client/app.js").read_text()
    assert "report" in concord.split("async function cordQuery(")[1].split("\n")[0], (
        "cordQuery must accept the report or neither read site can measure anything")
    assert concord.count("noteChannelReach(") >= 3, (
        "both the first history load and the live tick own the verdict for the channel they read")
    assert "authScope,report})" in concord, "the plane branch is the one Concord actually uses"
    assert "clearQueryCooldown(urls)" in relay
    assert "relayRetryRelays" in app, "the retry needs a bridge or the button cannot lift anything"
