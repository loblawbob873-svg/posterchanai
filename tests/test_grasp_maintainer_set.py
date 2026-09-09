"""GRASP-01's "recursive maintainer set" — transcribed from an implementation, not from a spec.

GRASP-01 says a server "MUST accept pushes … respecting the recursive maintainer set" and then
defines the term nowhere. NIP-34 defines only a flat `maintainers` tag. The real rule therefore lives
in ngit's code, so these tests are written against what ngit-cli 3.0.0 ACTUALLY DOES — cloned and
read at `src/lib/repo_ref.rs` and `src/lib/client.rs:2121`
(`get_repo_ref_from_cache_with_selected_recovery`) — and each test names the rule it pins.

Every case runs the SHIPPED `git_auth` functions against a stub cursor holding REAL signed events.
"""
from __future__ import annotations

import json
import os
import sys


_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import git_auth                      # noqa: E402
from app.services.nostr import bip340                  # noqa: E402
from app.services.nostr.event import build_event       # noqa: E402

REPO = "demo"
SK = {name: (n).to_bytes(32, "big") for n, name in
      enumerate(("owner", "b", "c", "d", "mod", "stranger"), start=11)}
PK = {name: bip340.pubkey_from_seckey(sk).hex() for name, sk in SK.items()}


def ann(author, tags, repo=REPO):
    """A really-signed 30617 for `repo` by `author`."""
    return build_event(SK[author], git_auth.ANNOUNCE_KIND, "", tags=[["d", repo]] + list(tags))


class _Cur:
    def __init__(self, conn):
        self._conn, self._rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        # git_auth's read is (repo_id, kind, pubkey). Answer per AUTHOR, which is the whole point:
        # the walk must be seen to ask about one pubkey at a time.
        repo_id, _kind, pubkey = params
        self._conn.asked.append(pubkey)
        self._rows = [(json.dumps(e),) for e in self._conn.events
                      if e["pubkey"] == pubkey
                      and any(t[:2] == ["d", repo_id] for t in e["tags"])]
        self._rows.sort(key=lambda r: -json.loads(r[0])["created_at"])

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, events):
        self.events, self.asked = list(events), []
        self.autocommit = False

    def cursor(self):
        return _Cur(self)

    def close(self):
        pass


def resolve(events, owner="owner"):
    return git_auth.load_maintainers(_Conn(events), PK[owner], REPO)


# ------------------------------------------------------------------ role tag activity (repo_ref.rs)

def test_a_role_tag_with_no_boundaries_is_active():
    """ngit `role_entry_is_active`: no history means active from the beginning."""
    assert git_auth._role_entry_active(["m", PK["b"]]) is True


def test_an_odd_number_of_boundaries_is_an_open_interval():
    assert git_auth._role_entry_active(["m", PK["b"], "1000"]) is True
    assert git_auth._role_entry_active(["m", PK["b"], "1000", "2000", "3000"]) is True


def test_an_even_number_of_boundaries_is_a_closed_interval():
    """start,end — the role ended. This is how somebody LEAVES."""
    assert git_auth._role_entry_active(["m", PK["b"], "1000", "2000"]) is False


def test_defer_is_legal_only_as_the_final_end_value():
    assert git_auth._role_entry_active(["m", PK["b"], "1000", "defer"]) is False   # closed, deferred
    assert git_auth._role_entry_active(["m", PK["b"], "defer"]) is False           # start position
    assert git_auth._role_entry_active(["m", PK["b"], "defer", "2000"]) is False   # not final


def test_a_non_numeric_boundary_invalidates_the_record():
    """"Any other non-numeric boundary makes the record invalid and therefore unable to grant
    authority" (repo_ref.rs:240-242). Fail-closed: unparseable must not mean allowed."""
    assert git_auth._role_entry_active(["m", PK["b"], "yesterday"]) is False


def test_a_moderator_is_never_a_maintainer():
    """"deliberately excluded from `maintainers`: per NIP-34 they can never … reach the state-event
    authority checks" (repo_ref.rs:84-86)."""
    maints, mods = git_auth.announcement_roles(ann("owner", [["o", PK["mod"]]]))
    assert maints == set() and mods == {PK["mod"]}


def test_role_tags_make_the_legacy_maintainers_tag_ignored():
    """"when any role tag is present the deprecated `maintainers` tag is ignored"
    (repo_ref.rs:73-76). Merging them would resurrect a maintainer whose role entry was closed."""
    ev = ann("owner", [["m", PK["b"]], ["maintainers", PK["stranger"]]])
    maints, _ = git_auth.announcement_roles(ev)
    assert maints == {PK["b"]}, "the legacy tag must not add anyone beside a role tag"


def test_the_legacy_maintainers_tag_still_works_on_its_own():
    """Every repo announced before role tags existed — including ours — uses only this."""
    maints, _ = git_auth.announcement_roles(ann("owner", [["maintainers", PK["b"], PK["c"]]]))
    assert maints == {PK["b"], PK["c"]}


# ------------------------------------------------------------------ the discovery loop (client.rs)

def test_the_owner_is_a_maintainer_with_no_announcement_at_all():
    assert resolve([]) == {PK["owner"]}


def test_one_level_still_works():
    assert resolve([ann("owner", [["maintainers", PK["b"]]])]) == {PK["owner"], PK["b"]}


def test_the_set_RECURSES_into_a_maintainers_own_announcement():
    """THE feature. The owner names B; B's own announcement names C; C is a maintainer. One level
    only — which is what we did before — refuses C's pushes."""
    got = resolve([ann("owner", [["m", PK["b"]]]), ann("b", [["m", PK["c"]]])])
    assert got == {PK["owner"], PK["b"], PK["c"]}


def test_it_recurses_further_than_one_extra_hop():
    got = resolve([ann("owner", [["m", PK["b"]]]),
                   ann("b", [["m", PK["c"]]]),
                   ann("c", [["m", PK["d"]]])])
    assert got == {PK["owner"], PK["b"], PK["c"], PK["d"]}


def test_a_STRANGERS_announcement_grants_nothing():
    """RULE 3, AND THE WHOLE SECURITY ARGUMENT. Only an author already IN the set expands it, so a
    30617 at a different coordinate injects nobody. Authority flows outward from the owner and can
    never flow inward — which is exactly what the old `WHERE pubkey = owner` was protecting."""
    got = resolve([ann("stranger", [["m", PK["stranger"]], ["m", PK["d"]]])])
    assert got == {PK["owner"]}, "a self-declared maintainer got in"


def test_a_moderators_announcement_assigns_nobody():
    """"a moderator's … assignments assign nothing, so their announcement is consulted solely for
    their own self-entries" (client.rs:2164-2167). They ARE walked — just not obeyed."""
    events = [ann("owner", [["o", PK["mod"]]]), ann("mod", [["m", PK["d"]], ["o", PK["mod"]]])]
    conn = _Conn(events)
    got = git_auth.load_maintainers(conn, PK["owner"], REPO)
    assert got == {PK["owner"]}
    assert PK["mod"] in conn.asked, "the moderator's announcement must still be READ"


def test_a_maintainer_who_ENDED_their_own_role_is_dropped():
    """Step 5, and the only rule in the walk that removes authority: "A member's own announcement
    takes precedence over assignments in other members' announcements" (client.rs:2192-2196).
    Without it, somebody who resigned keeps push access for ever."""
    events = [ann("owner", [["m", PK["b"]]]),
              ann("b", [["m", PK["b"], "1000", "2000"]])]      # closed self-role = left
    assert resolve(events) == {PK["owner"]}


def test_acknowledging_only_MODERATORSHIP_is_also_declining():
    """An author whose only self-entry is `o` is not asserting maintainership, even though the
    owner assigned them `m`."""
    events = [ann("owner", [["m", PK["b"]]]), ann("b", [["o", PK["b"]]])]
    assert resolve(events) == {PK["owner"]}


def test_an_author_named_by_NO_role_tag_has_not_declined():
    """"An author absent from all role tags has *not* declined — they are implicitly a maintainer"
    (repo_ref.rs:604-606). This is what keeps a legacy repo whose maintainers never publish working."""
    events = [ann("owner", [["m", PK["b"]]]), ann("b", [["name", "b's copy"]])]
    assert resolve(events) == {PK["owner"], PK["b"]}


def test_the_OWNER_is_never_dropped_by_a_decline():
    """A DELIBERATE DIVERGENCE FROM ngit, recorded here so it is not read as a bug. ngit resolves
    "who speaks for this repository"; we answer "who may write into THIS directory", and the npub in
    the URL is the directory. Obeying a self-decline here leaves a repo on disk that nobody can push
    to, recoverable only by an operator with a shell."""
    assert resolve([ann("owner", [["m", PK["owner"], "1000", "2000"]])]) == {PK["owner"]}


def test_a_departed_maintainers_own_assignments_go_with_them():
    """B leaves, so B's promotion of C must not survive them — otherwise resigning grants a
    permanent back door."""
    events = [ann("owner", [["m", PK["b"]]]),
              ann("b", [["m", PK["b"], "1000", "2000"], ["m", PK["c"]]])]
    got = resolve(events)
    assert PK["b"] not in got
    assert PK["c"] not in got, "a resigned maintainer's assignment outlived them"


def test_only_the_LATEST_announcement_per_author_speaks():
    """NIP-01 addressable rules — "a stale version could keep an ended role assignment active or
    hide a newer acknowledgement, leave or return" (client.rs:2186-2190)."""
    old = build_event(SK["owner"], git_auth.ANNOUNCE_KIND, "",
                      tags=[["d", REPO], ["m", PK["b"]]], created_at=1000)
    new = build_event(SK["owner"], git_auth.ANNOUNCE_KIND, "",
                      tags=[["d", REPO], ["m", PK["c"]]], created_at=2000)
    assert resolve([old, new]) == {PK["owner"], PK["c"]}


def test_a_tampered_announcement_speaks_for_nobody():
    bad = ann("owner", [["m", PK["b"]]])
    bad["tags"] = [["d", REPO], ["m", PK["stranger"]]]     # id/sig now describe different tags
    assert resolve([bad]) == {PK["owner"]}


def test_an_announcement_for_a_DIFFERENT_repo_is_not_consulted():
    assert resolve([ann("owner", [["m", PK["b"]]], repo="other")]) == {PK["owner"]}


def test_the_walk_terminates_on_a_cycle():
    """A and B naming each other is an ordinary two-maintainer repo, not an error — but it is also
    the shape that loops for ever without a fixpoint check."""
    events = [ann("owner", [["m", PK["b"]]]), ann("b", [["m", PK["owner"]]])]
    assert resolve(events) == {PK["owner"], PK["b"]}


def test_the_walk_is_BOUNDED_because_the_spec_supplies_no_bound(monkeypatch):
    """This runs from an UNAUTHENTICATED clone and every round is a Postgres read per newly
    discovered pubkey. ngit's own loop runs to a fixpoint over a local cache where that costs
    nothing; here it must not be a remote party's lever on our database."""
    monkeypatch.setattr(git_auth, "_MAINTAINER_MAX_PUBKEYS", 3)
    wide = ann("owner", [["m", PK[n]] for n in ("b", "c", "d", "mod", "stranger")])
    got = resolve([wide])
    assert len(got) <= 3, got


def test_a_long_chain_stops_at_the_round_ceiling(monkeypatch):
    monkeypatch.setattr(git_auth, "_MAINTAINER_MAX_ROUNDS", 2)
    events = [ann("owner", [["m", PK["b"]]]),
              ann("b", [["m", PK["c"]]]),
              ann("c", [["m", PK["d"]]])]
    got = resolve(events)
    assert PK["d"] not in got, "the ceiling did not hold"
    assert PK["b"] in got, "…but the rounds it did run must still count"


# ------------------------------------------------------ the set we did NOT adopt, and why (measured)

def test_our_own_production_shape_still_resolves_both_maintainers():
    """THE COMPATIBILITY FLOOR, and it is a real event, not a hypothetical.

    Measured on this node's relay: `d=posterchanai` has exactly ONE kind-30617, authored by
    `4b56bbf4…` (the author npub), carrying a LEGACY `maintainers` tag listing itself and
    `2829917e…` (the hosting node's operator key). The operator key has published NO announcement of
    its own for this repository.

    That matters because ngit v3's AUTHORITY set is not the set implemented here. `RepoRef::
    confirmed_maintainers` (repo_ref.rs:2121) requires MUTUAL confirmation — "a listed pubkey is only
    invited until their own announcement makes the relationship reciprocal, and an invited pubkey's
    events MUST NOT be treated as authoritative" (repo_ref.rs:2108-2111) — and it is what
    `client.rs:2588` assigns to `authorized_state_authors`, i.e. exactly our question. Under that
    rule `2829917e…` is an INVITEE, and `pre-receive` would refuse every push signed by the operator
    key, which is what `sync.sh` deploys production with.

    So this implements ngit's CANDIDATE set (`ordered_maintainers`, client.rs:2216) plus the one
    authority rule that costs nothing (a departing member appoints nobody). Adopting mutual
    confirmation is a live decision with a measured production cost, not a refactor.
    """
    author, operator = PK["owner"], PK["b"]
    legacy = ann("owner", [["maintainers", author, operator]])
    assert resolve([legacy]) == {author, operator}


def test_mutual_confirmation_is_NOT_required_by_this_implementation():
    """Stated as a test so the divergence is discoverable rather than folklore: B never publishes
    anything and is still a maintainer here. Under ngit's confirmed set B would not be."""
    assert PK["b"] in resolve([ann("owner", [["m", PK["b"]]])])
