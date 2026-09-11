""""NO INBOX" WAS A CLAIM ABOUT SOMEBODY ELSE'S ACCOUNT THAT WE HAD NOT EARNED.

Reported as users being unable to DM this account, one of them from Armada. Measured against the
live network rather than guessed, and it was three separate defects:

  1. WE NEVER PUT OUR OWN INBOX LIST WHERE ANYONE LOOKS. `ensureDmInboxList` published the kind-10050
     to our own relay pool only. Every other client resolves a DM inbox from the relay-list INDEXERS
     — the same set we read theirs from. Measured on this account: the 10050 was on damus/nos/primal
     by incidental sync and ABSENT from user.kindpag.es, which held our kind-10002. A client that
     (correctly) refuses to deliver a NIP-17 DM to a non-inbox relay therefore answers "no inbox"
     about an account that has one.

  2. THE DISCOVERY LIST WAS THREE-QUARTERS DEAD: purplepag.es 502, relay.0xchat.com 502,
     relay.nostr.band no answer, and only user.kindpag.es up — the one without the 10050. Worse,
     relay.damus.io, which DOES hold these lists, is on the pool's blocked-host set, so the one
     reachable relay with the answer was the one we refused to ask.

  3. AND "COULD NOT ASK" WAS RECORDED AS "HAS NO INBOX". The lookup sat in `catch(_){}`, the empty
     result was cached for an HOUR, and the sender was told "recipient has no DM inbox relays" —
     then the same verdict was served to the next person who tried. This repo already writes that
     rule down for the drive check, the uptime doc and the contacts reconcile; this path never got
     it.

(3) is what made it stick and what made the message false, so it is the one measured by RUNNING the
shipped lookup against relays that answer nothing.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _fn(head):
    i = APP.index(head)
    j = APP.index("{", i)
    depth = 0
    for k in range(j, len(APP)):
        if APP[k] == "{":
            depth += 1
        elif APP[k] == "}":
            depth -= 1
            if depth == 0:
                return APP[i:k + 1]
    raise AssertionError("unterminated " + head)


def _discovery():
    block = APP.split("const DISCOVERY_RELAYS = [", 1)[1].split("]", 1)[0]
    return [h for h in re.findall(r"'([^']+)'", block)]


def test_our_own_inbox_list_is_published_where_other_clients_read_it():
    """DEFECT 1. Reading their list from the indexers and writing ours only to our own relay is not
    a protocol, it is a one-way mirror."""
    body = _fn("async function ensureDmInboxList()")
    assert "publish(10050" in body, "we no longer publish a DM inbox list at all"
    assert "publishTo(DISCOVERY_RELAYS" in body, (
        "our kind-10050 goes to our own pool only again — the indexers every other client resolves "
        "a DM inbox from will not have it, and those clients answer 'no inbox'")
    assert body.index("publish(10050") < body.index("publishTo(DISCOVERY_RELAYS"), (
        "the broadcast runs before the event exists")
    assert ".catch(" in body.split("publishTo(DISCOVERY_RELAYS", 1)[1][:200], (
        "a dead indexer must not fail a write that already succeeded")


def test_the_discovery_set_is_not_one_outage_wide():
    """DEFECT 2. Four hosts that can all be down at once is not a fallback — and three of the
    original four were down when this was measured."""
    hosts = _discovery()
    assert len(hosts) >= 6, "the discovery set is back to a handful of specialist indexers: %r" % hosts
    for needed in ("relay.damus.io", "nos.lol", "relay.primal.net"):
        assert any(needed in h for h in hosts), (
            "%s is not in the discovery set — it is one of the relays that actually holds these "
            "lists" % needed)


def test_a_blocked_but_reachable_relay_is_still_asked_for_a_relay_list():
    """relay.damus.io is on the pool's blocked-host set (it is a firehose we do not want in the
    shared pool). That is right for the pool and wrong for a one-author, two-kind, limit-2 read —
    it made the one reachable relay holding the answer the one we refused to ask."""
    body = _fn("async function dmInboxRelays(pk)")
    assert "allowBlocked:true" in body.replace(" ", ""), (
        "the inbox lookup refuses the blocked hosts again, including the one that has the data")
    relay = (ROOT / "static/js/client/relay.js").read_text(encoding="utf-8")
    assert "relay.damus.io" in relay.split("_queryFromBlockedHosts", 1)[1][:200], (
        "re-point this test: damus is no longer on the blocked-host set, so the opt-out above may "
        "no longer be needed")


def test_the_lookup_reports_whether_anybody_actually_answered():
    """DEFECT 3, in source: the caller cannot tell the two apart unless the lookup says so."""
    body = _fn("async function dmInboxRelays(pk)")
    assert "report" in body and "report.ok" in body.replace(" ", "").replace("(report.ok||[])", "report.ok"), (
        "the lookup no longer asks queryFrom which relays replied, so an outage is indistinguishable "
        "from an empty answer")
    assert "answered" in body
    assert "if(answered)" in body.replace(" ", ""), (
        "an empty result is cached again — an outage becomes a verdict about the recipient that "
        "lasts an hour")


def test_the_two_failures_do_not_share_a_sentence():
    """The user-visible half. One of these is about the recipient and the other is about us."""
    send = APP[APP.index("dmInboxRelays(pk).then("):][:1400]
    assert "recipient has no DM inbox relays" in send
    assert "could not look up" in send, (
        "an unreachable lookup still tells the sender the recipient has no inbox — a confident "
        "claim about somebody else's account, made after reaching nobody")
    assert "answered" in send, "the sentence is not chosen by what actually happened"


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_an_outage_is_not_cached_as_a_verdict():
    """DEFECT 3, RUN. The shipped lookup against relays that answer nothing: it must report that
    nobody answered, and it must NOT remember the emptiness — otherwise the next person to try gets
    an hour-old verdict that was never anybody's answer.

    Then the same lookup with a relay that does reply and genuinely holds nothing: THAT is a real
    empty answer, and caching it is right."""
    body = _fn("async function dmInboxRelays(pk)")
    picks = _fn("function _pick10050(evs, pk)") + "\n" + _fn("function _pick10002(evs, pk)")
    script = """
      const _inboxCache = new Map(), _INBOX_TTL = 3600*1000;
      const normalizeRelay = (u) => String(u || '').trim();
      const DISCOVERY_RELAYS = ['wss://a/', 'wss://b/'];
      let mode = 'outage', asked = 0;
      const Relay = {
        query: async () => [],
        queryFrom: async (urls, filters, opts) => {
          asked++;
          if (mode === 'outage') { opts.report.failed = urls.slice(); return []; }
          opts.report.ok = urls.slice();                 // somebody answered
          return mode === 'found'
            ? [{ id: 'e1', kind: 10050, pubkey: filters[0].authors[0],
                 tags: [['relay', 'wss://inbox.example/']], created_at: 1 }]
            : [];
        },
        worker: { call: async (_, { events }) => events.map(e => ({ id: e.id, valid: true })) },
      };
      %s
      %s
      (async () => {
        const out = {};
        out.outage = await dmInboxRelays('PK');
        out.outageAskedAgain = (await dmInboxRelays('PK'), asked);   // must have looked AGAIN
        mode = 'empty'; asked = 0;
        out.empty = await dmInboxRelays('PK2');
        await dmInboxRelays('PK2');
        out.emptyAskedAgain = asked;                                  // must be cached: one ask
        mode = 'found'; asked = 0;
        out.found = await dmInboxRelays('PK3');
        console.log(JSON.stringify(out));
      })();
    """ % (picks, body)
    done = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])

    assert got["outage"]["relays"] == []
    assert got["outage"]["answered"] is False, (
        "every relay failed and the lookup still called it an answer — the sender is then told the "
        "recipient has no inbox")
    assert got["outageAskedAgain"] == 2, (
        "the outage was cached, so the next attempt inherits a verdict nobody gave (for an hour)")

    assert got["empty"]["answered"] is True, "a relay replied; that IS an answer"
    assert got["emptyAskedAgain"] == 1, "a real empty answer is not being cached, so every send re-asks"

    assert got["found"]["relays"] == ["wss://inbox.example/"]
    assert got["found"]["answered"] is True
