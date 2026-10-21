"""GRASP-01 MUST: "accept other events that tag accepted git events".

Measured against the spec (grasp.git 01.md @ f35b4f9, 2026-10-21): events that reach an issue, patch or
PR through an `e`/`E`/`q` tag — and not through the repo's `a` tag — fell to the web-of-trust gate and
were refused `blocked: not in web of trust` from anyone outside it. That is:
  * a STATUS (1630-1633) without the `a` tag, which NIP-34 only recommends: a non-ngit client's close
    or reopen was lost;
  * a deletion, reaction, label or repost of an issue: a contributor outside the WoT could not even
    delete their own PR.

Drives the SHIPPED `_on_event` with real signed events, reusing the PR-kinds test's fixtures.
"""
from __future__ import annotations

import pytest

from app.services.nostr.event import build_event
from tests.test_grasp_pr_kinds_accepted import OWNER, REPO, STRANGER_SK, deliver, relay

ISSUE = build_event(bytes.fromhex("22" * 32), 1621, "it breaks", [["a", f"30617:{OWNER}:{REPO}"], ["subject", "it breaks"]])
UNKNOWN_ID = "f" * 64


def with_issue(announced=True):
    s = relay(announced=announced)

    async def query(filters, hard_cap=None):
        ids = filters[0].get("ids") or []
        return [ISSUE] if ISSUE["id"] in ids and ISSUE["kind"] in filters[0].get("kinds", [ISSUE["kind"]]) else []
    s.store.query = query
    return s


@pytest.mark.parametrize("kind", [1630, 1631, 1632, 1633])
def test_a_status_without_an_a_tag_is_accepted_through_its_root(kind):
    s = with_issue()
    ok = deliver(s, build_event(STRANGER_SK, kind, "", [["e", ISSUE["id"], "", "root"]]))
    assert ok[2] is True, ok
    assert s.store.added


@pytest.mark.parametrize("kind,tags", [
    (5, [["e", ISSUE["id"]], ["k", "1621"]]),
    (7, [["e", ISSUE["id"]], ["p", "b" * 64]]),
    (1985, [["L", "ugc"], ["l", "bug", "ugc"], ["e", ISSUE["id"]]]),
    (16, [["q", ISSUE["id"]], ["k", "1621"]]),
])
def test_events_that_tag_an_accepted_issue_are_accepted(kind, tags):
    s = with_issue()
    ok = deliver(s, build_event(STRANGER_SK, kind, "+" if kind == 7 else "", tags))
    assert ok[2] is True, (kind, ok)


def test_tagging_an_unknown_event_is_still_web_of_trust_gated():
    s = with_issue()
    ok = deliver(s, build_event(STRANGER_SK, 7, "+", [["e", UNKNOWN_ID]]))
    assert ok[2] is False and "web of trust" in ok[3]


def test_tagging_an_issue_of_a_repo_no_longer_announced_is_refused():
    s = with_issue(announced=False)
    ok = deliver(s, build_event(STRANGER_SK, 1632, "", [["e", ISSUE["id"], "", "root"]]))
    assert ok[2] is False


def test_a_note_quoting_an_issue_is_not_let_in_by_this_rule():
    """Kind 1 is social content: quoting an issue must not become a way around the WoT."""
    s = with_issue()
    ok = deliver(s, build_event(STRANGER_SK, 1, "look", [["q", ISSUE["id"]]]))
    assert ok[2] is False and "web of trust" in ok[3]


def test_a_tag_stuffed_event_costs_at_most_three_lookups():
    s = with_issue()
    calls = []
    orig = s.store.query

    async def counting(filters, hard_cap=None):
        calls.append(filters)
        return await orig(filters, hard_cap)
    s.store.query = counting
    tags = [["e", f"{i:064x}"] for i in range(50)] + [["e", ISSUE["id"]]]
    ok = deliver(s, build_event(STRANGER_SK, 7, "+", tags))
    assert len(calls) <= 3, len(calls)
    assert ok[2] is False, "the 51st tag must never be looked at"
