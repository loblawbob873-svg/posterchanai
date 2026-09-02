"""WHERE A USER'S OWN MONERO INFORMATION IS KEPT, AND WHAT MAY LEAVE THE DEVICE.

The wallet tests elsewhere are about the NODE's wallet — the RPC wallet, its caps, its ledger. This
file is about the other half, which is stored somewhere else entirely and by different rules: the
things that belong to the person using the client.

There are exactly three stores, and they have three different privacy contracts:

  1. **The address itself** → their own kind-0 profile, under four flat aliases plus Garnet's
     `cryptocurrency_addresses` map. Public by definition. Covered for read/write/clear by
     `tests/test_monero_profile_fields.py`; what is added here is that clearing really does reach
     every alias at once, because a stale address left under one key is money sent to the wrong
     place by whichever client reads that key.
  2. **Tip presets and the last amount** → `ClientSettings`, mirrored to the kind-30078
     `pcai:client-prefs` document, which is published **in PLAINTEXT**. Anything put there is
     readable by anyone, for ever, attributed to that pubkey.
  3. **"Attach my Monero address to every post"** (`xmrStampNotes`) → `ClientSettings` and NOWHERE
     ELSE, deliberately. It is an address-linking privacy decision, and syncing it would turn it on
     for a device whose owner never agreed to it there.

The failures these guard against are all silent:

  * A key that should never sync joining the plaintext prefs document.
  * A prefs save republishing the document from a read that never happened — the replaceable-doc
    wipe this repo has paid for several times. Saving a Monero tip amount on a bad connection would
    take the zap presets, the BCH presets and the data-saver with it.
  * The public tip note carrying more than the tipper meant to publish.
  * The node's spend ledger recording WHO was paid, which would be a deanonymisation log kept
    server-side for the one feature whose entire premise is that payments are private.

Every function is LIFTED from the shipped `app.js` and run — never restated here.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
RELAY = os.path.join(ROOT, "static", "js", "client", "relay.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

ADDR = ("47ik6ZUx9MkfTyt9sZRbJk8SJAXCcj44t2vhenUJSAPiB1SJxZSRysvRbMQLVR26"
        "yHcF6UHcgFUTsigJ4DHDMyGaAkgEuzi")


def _src():
    return open(APP, encoding="utf-8").read()


def _lift(name, src=None):
    """The real function out of app.js, `async` or not — never a restatement of it here."""
    src = src if src is not None else _src()
    m = re.search(r"\n  (?:async )?function " + re.escape(name) + r"\(.*?\n  \}", src, re.S)
    assert m, f"{name} is gone from app.js"
    return m.group(0)


def _run(js):
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "case.mjs")
        open(path, "w", encoding="utf-8").write(js)
        done = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)


# ===========================================================================================
# 2. The plaintext prefs document
# ===========================================================================================

#: The prefs save is a read-modify-write of one replaceable event. This harness supplies the two
#: things it touches — `Relay.query` (whose `complete` flag says whether the relays actually
#: answered) and `publish` — and records what it tried to write.
PREFS_HARNESS = """
globalThis.ME = { pubkey: 'ab'.repeat(32) };
globalThis.published = [];
globalThis.publish = async (kind, content, tags) => {
  published.push({ kind, content: JSON.parse(content), tags });
  return { ok: true };
};
globalThis.queries = 0;
globalThis.answer = { events: [], complete: true };
globalThis.Relay = {
  query: async () => {
    queries++;
    if (answer.throws) throw new Error('no relays');
    const got = answer.events.slice();
    if (answer.complete !== undefined) {
      Object.defineProperty(got, 'complete', { value: answer.complete, enumerable: false });
    }
    return got;
  },
};
"""


def _prefs(script, answer):
    src = _src()
    return _run(PREFS_HARNESS
                + "globalThis.answer = %s;\n" % json.dumps(answer)
                + _lift("_readPrefs", src)
                + "\n" + _lift("saveClientPrefsNostr", src)
                + "\nlet _prefsSaveChain = Promise.resolve();\n"
                + script)


def test_a_prefs_save_merges_the_patch_onto_what_is_already_there():
    """The ordinary path: remembering a Monero tip amount must not disturb the zap presets or the
    sidebar order sitting in the same document."""
    stored = {"zapPresets": "21, 100", "bchPresets": "0.001", "noImages": True}
    got = _prefs("""(async()=>{
      await saveClientPrefsNostr({ xmrTip: '0.25' });
      process.stdout.write(JSON.stringify({ published, queries }));
    })();""", {"events": [{"content": json.dumps(stored), "created_at": 1}], "complete": True})
    assert len(got["published"]) == 1
    assert got["published"][0]["kind"] == 30078
    assert got["published"][0]["tags"] == [["d", "pcai:client-prefs"]]
    assert got["published"][0]["content"] == {**stored, "xmrTip": "0.25"}


def test_a_first_save_with_no_document_yet_still_writes():
    """A read that COMPLETED and found nothing is a real answer: this account has no prefs document.
    Refusing here would mean a brand-new account could never sync a preset at all."""
    got = _prefs("""(async()=>{
      await saveClientPrefsNostr({ xmrPresets: '0.001, 0.01' });
      process.stdout.write(JSON.stringify({ published }));
    })();""", {"events": [], "complete": True})
    assert got["published"][0]["content"] == {"xmrPresets": "0.001, 0.01"}


def test_a_save_whose_read_never_completed_refuses_to_republish_the_document():
    """THE DATA LOSS. The document is REPLACEABLE: publishing a merge onto a read that timed out
    replaces everything in it with just this patch. Saving one Monero tip amount on a train would
    delete the zap presets, the BCH presets and the data-saver setting from every device.

    "No relay answered" and "there is no document" are the same empty array, and only the `complete`
    flag separates them — the identical rule the timeline, Trending and the uptime monitor follow."""
    got = _prefs("""(async()=>{
      await saveClientPrefsNostr({ xmrTip: '0.25' });
      process.stdout.write(JSON.stringify({ published, queries }));
    })();""", {"events": [], "complete": False})
    assert got["published"] == [], (
        "a prefs save republished the whole document from a read that never completed — every "
        "pref not in this patch is now gone")
    assert got["queries"] >= 1, "it did not even try to read"


def test_a_save_with_no_relays_at_all_writes_nothing():
    """Offline is the commonest version of the same thing, and the one where a user is most likely
    to be fiddling with settings."""
    got = _prefs("""(async()=>{
      await saveClientPrefsNostr({ xmrTip: '0.25' });
      process.stdout.write(JSON.stringify({ published }));
    })();""", {"events": [], "complete": True, "throws": True})
    assert got["published"] == []


def test_the_completeness_flag_the_guard_depends_on_is_still_set_by_relay_query():
    """The guard above reads `evs.complete === true`. If `Relay.query` ever stopped marking its
    results, that check would silently become "never publish" and prefs syncing would quietly stop
    working — so the flag is pinned where it is produced, not only where it is consumed."""
    relay = open(RELAY, encoding="utf-8").read()
    assert "Object.defineProperty(got, 'complete'" in relay
    assert "value: !viaTimeout" in relay


def test_the_address_linking_opt_in_is_never_sent_to_the_relay():
    """`xmrStampNotes` attaches the user's Monero address to EVERY post, which links all of their
    posts to one payment identifier. It is a per-device choice on purpose: syncing it would enable
    it on a phone whose owner never made that decision there. The prefs document is plaintext, so
    it would also announce publicly that this pubkey is doing it."""
    src = _src()
    for call in re.findall(r"saveClientPrefsNostr\(\s*\{[^}]*\}", src):
        assert "xmrStampNotes" not in call, f"the stamp opt-in is being synced: {call}"
    restore = _lift("restoreClientPrefsNostr", src)
    assert "xmrStampNotes" not in restore, (
        "a restore writes the stamp setting from the relay — that turns address-linking on for a "
        "device that never opted in")


def test_only_the_two_non_identifying_monero_prefs_ever_reach_the_plaintext_document():
    """Whatever is in `pcai:client-prefs` is public and permanent. The Monero keys allowed there are
    a preset list and a remembered amount; an address, a txid or a proof must never join them."""
    src = _src()
    synced = set()
    for call in re.findall(r"saveClientPrefsNostr\(\s*\{([^}]*)\}", src):
        synced.update(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", call))
    monero = {key for key in synced if "xmr" in key.lower() or "monero" in key.lower()}
    assert monero <= {"xmrTip", "xmrPresets"}, f"unexpected Monero keys synced in clear: {monero}"
    for forbidden in ("xmrAddr", "xmrAddress", "moneroAddress", "xmrTxid", "xmrProof",
                      "xmrStampNotes"):
        assert forbidden not in synced


# ===========================================================================================
# 1. The address in the profile
# ===========================================================================================


def test_clearing_the_address_reaches_every_alias_in_one_write():
    """A profile write is one replaceable event. If a clear removed the flat keys but left the map
    (or the reverse), the next client to read the surviving key tips an address the user has
    deliberately retired — and there is no second write coming to fix it."""
    src = _src()
    # Anchor on the SAVE, not on the first mention of the map (which is a comment 15k lines earlier).
    block = src[src.index("const _xmrWas = xmrOf(p)"):][:2500]
    assert "delete meta.xmr" in block and "delete meta.monero_address" in block
    assert "delete meta.monero;" in block
    assert "delete meta.cryptocurrency_addresses" in block, (
        "clearing does not remove the map form, so a Garnet reader keeps the old address")


def test_a_profile_write_keeps_other_peoples_coins_in_the_map():
    """`cryptocurrency_addresses` is shared with whatever client wrote it. Rebuilding it from our
    form would delete the user's Bitcoin or Litecoin address as a side effect of saving Monero."""
    src = _src()
    window = src[src.index("const _m = (p.cryptocurrency_addresses"):][:600]
    assert "Object.assign({}, p.cryptocurrency_addresses)" in window, (
        "the map is rebuilt rather than copied, so unmanaged coins are dropped")
    # And the clear only drops the whole map when Monero was the last coin left in it.
    clear = src[src.index("const _m = Object.assign({}, p.cryptocurrency_addresses);"):][:500]
    assert "if(Object.keys(_m).length) meta.cryptocurrency_addresses = _m;" in clear, (
        "clearing Monero deletes the map, taking every other coin in it with it")


# ===========================================================================================
# 3. What a tip publishes about the tipper
# ===========================================================================================


def test_the_public_tip_note_names_the_recipient_and_never_the_senders_address():
    """The note exists so the person tipped is told at all — Monero payments are private, so nothing
    else can tell them. What it must NOT do is publish the sender's own address: that would link
    the tipper's wallet to the tip for everyone, which is precisely the cost the `xmrStampNotes`
    opt-in exists to make the user choose deliberately."""
    body = _lift("_postXmrTipNote")
    assert "['p',pk]" in body.replace(" ", ""), "the recipient is not p-tagged, so they are never told"
    assert "xmrOf(" not in body and "ME.pubkey" not in body, (
        "the tip note reaches for the SENDER's own address")
    # The only address in the note is the recipient's, and only inside the verification proof, which
    # is useless without it (`check_tx_proof <txid> <addr> "" <proof>`).
    address_uses = re.findall(r"\baddr\b", body)
    assert address_uses, "the proof tag no longer carries the address it needs"
    assert "monero_proof" in body
    proof_line = next(line for line in body.splitlines() if "monero_proof" in line)
    assert "isXmrAddr(addr)" in proof_line, "an unvalidated string is published as an address"


def test_a_tip_note_is_only_posted_for_a_send_that_actually_happened():
    """`onSent` is the built-in wallet's success callback and fires after the server confirmed the
    transfer. A note posted on the attempt would publicly credit a payment that failed."""
    src = _src()
    # The options are built first now (both wallet paths share them), so anchor on where they are
    # DEFINED rather than on the first call site.
    window = src[src.index("const _tipOpts = {"):][:1600]
    # The callback also remembers the amount now, so it is a block rather than a one-liner. What
    # matters is unchanged: the note is posted from onSent — i.e. only for a send that happened.
    assert "onSent:(amount, txid)=>{" in window
    assert "_postXmrTipNote(noteId, pk, amount, addr, txid||'', '')" in window
    assert "prepare" not in window, "the note is posted from the preparation, not the confirmation"


def test_the_amount_published_is_never_a_zero_or_a_nan():
    """`amtVal()` is what goes into the note and into the payment URI. A `tx_amount=0` or a NaN in a
    public note is a claim about a payment nobody made."""
    src = _src()
    line = next(line for line in src.splitlines() if "const amtVal=()=>" in line)
    assert "isFinite(n)" in line and "n>0" in line
    assert "never tx_amount=0" in line


# ===========================================================================================
# The node-side record of a user's spending
# ===========================================================================================


def test_the_servers_spend_ledger_records_no_destination_address():
    """The one place this node keeps a durable record of a user's Monero activity. It exists to
    enforce the daily cap, so it needs a time, an account and an amount — and nothing else. Storing
    the destination would build, server-side and in the clear, exactly the payment graph Monero
    exists to prevent, for every tip anyone ever sent from this node."""
    service = open(os.path.join(ROOT, "app", "services", "monero_wallet_service.py"),
                   encoding="utf-8").read()
    create = re.search(r"CREATE TABLE IF NOT EXISTS monero_spend_attempts \(([^)]*)\)", service)
    assert create, "the spend ledger schema has moved"
    columns = {c.strip().split()[0] for c in create.group(1).split(",")}
    assert columns == {"at", "user_id", "amount_atomic"}, (
        f"the spend ledger grew a column: {columns}")
    insert = re.search(r"INSERT INTO monero_spend_attempts\(([^)]*)\)", service)
    assert {c.strip() for c in insert.group(1).split(",")} == {"at", "user_id", "amount_atomic"}
    assert "address" not in create.group(1) and "txid" not in create.group(1)
