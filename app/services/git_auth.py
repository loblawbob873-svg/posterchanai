"""GRASP git-over-nostr push authorization — the security crux (P1).

This module is the SINGLE source of truth for "may this ref update be written?". It is
deliberately import-light (stdlib + the repo's pure-Python nostr helpers + psycopg2) so it can
be pulled in from a bare `pre-receive` git hook subprocess (`git_hooks/pre_receive.py`) with just
the repo root on sys.path — no FastAPI, no settings hydration, no event loop.

CORE PRINCIPLE (fail-closed): a push to `refs/heads/<b>` is accepted ONLY if the resulting
ref->SHA mapping is backed by a Nostr **kind-30618** "repository state" event that

  1. is signed by an **authorized maintainer** of THIS repo, where the maintainer ACL is read
     ONLY from `30617:<owner-in-URL>:<id>` (owner + its `maintainers` tag) — a forged 30617 from
     a random pubkey addresses a DIFFERENT coordinate and can never self-authorize; and
  2. has its **BIP-340 signature re-verified right here**, never trusting the relay DB row's mere
     presence (defends against a poisoned/compromised `events` row); and
  3. is the **newest by created_at** among the maintainer-signed candidates (defeats replay of an
     old signed state to rewind the repo); and
  4. names EXACTLY the `<newsha>` git is trying to write for that ref (SHA-equality; git
     guarantees the pushed tip resolves to those objects, and receive.fsckObjects rejects
     malformed ones).

Any error, ambiguity, missing state, or mismatch -> reject. The caller (pre-receive) turns a
reject into a non-zero exit, so git discards the quarantined objects and nothing is written.

The decision function `decide_push_ref` is a PURE function of its inputs so it can be unit-tested
directly with crafted events (see tests/test_git_push_auth.py) — the actual security review target.
"""

import base64
import json
import time
from urllib.parse import urlparse

# Pure-Python NIP-01 verify (recomputes the id + checks the BIP-340 Schnorr sig). Import-safe in a
# hook: no side effects, no network, no DB.
from app.services.nostr.event import verify_event
from app.services.nostr import bech32

ZERO_SHA = "0" * 40
STATE_KIND = 30618        # NIP-34 repository state (the push-authorization token)
ANNOUNCE_KIND = 30617     # NIP-34 repository announcement (carries the maintainer ACL)
NIP98_KIND = 27235        # NIP-98 HTTP auth event


# --------------------------------------------------------------------------- helpers

def _norm_hex(pk) -> str | None:
    """Normalize a maintainer entry (hex or npub) to 64-char lowercase hex, else None."""
    if not isinstance(pk, str):
        return None
    s = pk.strip()
    if len(s) == 64:
        try:
            bytes.fromhex(s)
            return s.lower()
        except ValueError:
            return None
    if s.startswith("npub1"):
        raw = bech32.decode("npub", s)
        return raw.hex() if raw and len(raw) == 32 else None
    return None


def _is_sha(s) -> bool:
    if not isinstance(s, str) or len(s) != 40:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def refs_from_state(event: dict) -> dict:
    """Parse a kind-30618 event's tags into {refname: sha}. Only `refs/...` -> 40-hex-sha tags are
    taken; HEAD/other tags are ignored for the ref->SHA authorization map. Case-normalized SHAs."""
    refs: dict = {}
    for t in event.get("tags") or []:
        if len(t) >= 2 and isinstance(t[0], str) and t[0].startswith("refs/"):
            sha = str(t[1]).strip().lower()
            if _is_sha(sha):
                refs[t[0]] = sha
    return refs


def select_authorized_state(state_events, maintainers) -> dict | None:
    """From candidate 30618 events pick the authoritative one: authored by a maintainer, kind 30618,
    with its BIP-340 signature RE-VERIFIED here (never trust DB presence), newest by created_at.

    Returns the chosen event dict, or None if no candidate is valid+authorized."""
    best = None
    for ev in state_events or []:
        try:
            if not isinstance(ev, dict):
                continue
            if int(ev.get("kind", 0)) != STATE_KIND:
                continue
            if ev.get("pubkey") not in maintainers:      # ACL gate (defense in depth vs the SQL filter)
                continue
            if not verify_event(ev):                     # BIP-340 re-verify — poisoned rows die here
                continue
            if best is None or int(ev.get("created_at", 0)) > int(best.get("created_at", 0)):
                best = ev                                # newest maintainer-signed wins (anti-replay)
        except (ValueError, TypeError):
            continue
    return best


def decide_push_ref(ref: str, old_sha: str, new_sha: str, maintainers,
                    state_events, *, allow_force: bool = True,
                    is_non_fast_forward: bool = False,
                    nip98_signer: str | None = None) -> tuple[bool, str]:
    """THE push-authorization decision for a single ref line. Pure function; fail-closed.

    Args:
      ref                  git ref being updated, e.g. "refs/heads/main".
      old_sha, new_sha     the receive-pack ref line SHAs ("0"*40 == create/delete sentinel).
      maintainers          set of authorized maintainer pubkeys (hex) = owner ∪ 30617.maintainers.
      state_events         candidate kind-30618 events (raw dicts) for 30618:<owner>:<id>.
      allow_force          if False, a non-fast-forward update is rejected even when signed.
      is_non_fast_forward  computed by the caller (git merge-base --is-ancestor); tests pass directly.
      nip98_signer         a verified NIP-98 maintainer pubkey (convenience/admin path), or None.

    Returns (accepted, reason). `accepted=False` MUST cause the caller to exit non-zero.
    """
    # (0) NIP-98 authenticated-maintainer bypass. A maintainer who signed a fresh NIP-98 header for
    # THIS receive-pack URL is trusted to push arbitrary refs; post-receive derives the 30618 from
    # what actually landed. This is the automation/sync.sh path — still gated on the maintainer ACL.
    if nip98_signer and nip98_signer in maintainers:
        return True, "nip98: authenticated maintainer %s" % nip98_signer[:12]

    # (1) Load the authoritative signed state: newest maintainer-signed 30618, signature re-verified.
    state = select_authorized_state(state_events, maintainers)
    if state is None:
        # No 30618 present, or none from a maintainer, or all had bad signatures -> reject.
        return False, "no valid signed 30618 repo-state from an authorized maintainer"

    state_refs = refs_from_state(state)
    want = state_refs.get(ref)

    # (2) Delete: allowed only if the signed state ALSO drops this ref (want is None).
    if new_sha == ZERO_SHA:
        if want is not None:
            return False, "delete of %s not reflected in signed state (state still pins %s)" % (ref, want[:12])
        return True, "delete of %s authorized (absent from signed state)" % ref

    # (3) The ref must be named by the signed state.
    if want is None:
        return False, "%s is not present in the signed 30618 state" % ref

    # (4) SHA-equality — the crux. The tip git wants to write must be EXACTLY what a maintainer signed.
    if want != new_sha:
        return False, "%s target %s != signed %s" % (ref, new_sha[:12], want[:12])

    # (5) Force-push policy. new==want here, but the update may still rewrite history (non-ff).
    # A maintainer signed the new state, so it's authorized; gate it behind allow_force + log upstream.
    if is_non_fast_forward and not allow_force:
        return False, "%s is a non-fast-forward (force-push) and git_server_allow_force is off" % ref

    return True, "%s authorized by signed 30618 (%s)" % (ref, new_sha[:12])


# --------------------------------------------------------------------------- NIP-98

def verify_nip98(header: str | None, method: str | None, repo_path_needle: str,
                 allowed, *, max_skew: int = 60, require_method: bool = True,
                 allow_basic: bool = False) -> str | None:
    """Verify a NIP-98 (kind 27235) `Authorization: Nostr <base64-event>` header. Fail-closed.

    Returns the signer's hex pubkey iff ALL hold, else None:
      - valid base64 -> kind-27235 event whose BIP-340 signature re-verifies here;
      - (if require_method) the `method` tag equals `method`;
      - the `u` tag's path CONTAINS `repo_path_needle` (binds the header to THIS repo — blocks
        cross-repo replay). For push we pass "<id>.git/git-receive-pack" (also blocks reusing a
        read-scoped upload-pack header to authorize a write); for read we pass "<id>.git";
      - created_at within ±max_skew of now (the replay window; GRASP-08 says 60s, which is the
        default on both gates now — `git_server_read_skew` widens it for a client that needs it);
      - the signer pubkey is in `allowed`.

    THE `method` ARGUMENT IS WHAT THE TAG MUST SAY, not what the request did. The read gate passes
    the literal "GET" with require_method=True — GRASP-08's "one credential covering all endpoints of
    a Smart HTTP operation, method tag GET" — because a `git clone` sends the SAME static
    `http.extraHeader` for the info/refs GET and the upload-pack POST, so comparing the tag to the
    request's verb would 401 the second half of every clone. (It is also what
    `scripts/git-credential-nostr` has always signed: git hands a credential helper no method to
    echo, so it emits `method: GET` unconditionally.) `require_method=False` remains available for an
    operator whose client signs something else — `git_server_read_require_method`. Push passes the
    real verb, matched exactly (writes are higher-stakes).

    allow_basic=True additionally accepts the SAME base64 event carried as the password half of an
    `Authorization: Basic <b64 user:pass>` header, so any client that can only do username/password
    can still present a NIP-98 token — `scripts/git-credential-nostr` mints a fresh one per request.
    Every check below is unchanged, so this is a second envelope for the same signed token, not a
    second way to authenticate. Enabled for the READ gate only — push never sets it.

    This is what lets BOTH plain `git clone https://…/<id>.git` and ngit read a private repo. Stock
    ngit 2.6.3 never invoked the helper (measured with a logging helper: zero calls) because it only
    attempts the *unauthenticated* protocol against a GRASP server, so the credentials callback is
    never installed — a locally patched ngit adds the authenticated fallback. See
    docs/GIT_OVER_NOSTR.md; ngit sends no NIP-98 of its own, so this envelope is the way in.

    NOTE on replay: the ±max_skew freshness window plus URL binding is the practical guard; a nonce
    cache isn't feasible across independent one-shot hook processes / stateless request handlers. The
    header alone can't push without valid git objects matching the signed state, and it's bound to one
    repo + a short time window. Documented as an accepted limitation.
    """
    if not header:
        return None
    try:
        parts = header.strip().split(None, 1)
        if len(parts) != 2:
            return None
        scheme = parts[0].lower()
        if scheme == "nostr":
            b64_event = parts[1]
        elif allow_basic and scheme == "basic":
            # ngit's transport is libgit2 and cannot emit `Authorization: Nostr`, but it DOES run
            # git credential helpers, which can only return a username/password pair. So carry the
            # very same base64 NIP-98 event as the Basic *password* (`git-credential-nostr` mints a
            # fresh one per request). Nothing is weakened: the event below is still BIP-340 verified,
            # bound to this repo by its `u` tag, freshness-checked and ACL-checked. Reads only.
            userpass = base64.b64decode(parts[1], validate=True).decode("utf-8")
            if ":" not in userpass:
                return None
            b64_event = userpass.split(":", 1)[1]
        else:
            return None
        raw = base64.b64decode(b64_event, validate=True).decode("utf-8")
        ev = json.loads(raw)
        if not isinstance(ev, dict) or int(ev.get("kind", 0)) != NIP98_KIND:
            return None
        if not verify_event(ev):                      # BIP-340 re-verify
            return None
        tags = {}
        for t in ev.get("tags") or []:
            if len(t) >= 2 and isinstance(t[0], str) and t[0] not in tags:
                tags[t[0]] = t[1]
        if require_method and str(tags.get("method", "")).upper() != (method or "").upper():
            return None
        u = str(tags.get("u", ""))
        path = urlparse(u).path if "://" in u else u
        if repo_path_needle not in path:
            return None                                # header not bound to THIS repo -> reject
        if abs(int(time.time()) - int(ev.get("created_at", 0))) > max_skew:
            return None                                # stale/future -> replay guard
        pk = ev.get("pubkey")
        return pk if pk in allowed else None
    except (ValueError, TypeError, KeyError):
        return None


# --------------------------------------------------------------------------- GRASP-08 privacy

def event_says_private(event) -> bool:
    """GRASP-08: a repository is PRIVATE when its kind-30617 announcement carries ["private","true"].

    THE ONE definition of that predicate, deliberately here in the import-light module both halves
    already depend on. The relay's serve gate (nostr_relay/server.py:_is_private_repo_event) and the
    git HTTP read gate (git_host_main.py:_announced_private) both call it, because two hand-written
    copies of "does this announcement say private" is exactly how a repo ends up refused at one door
    and served at the other — and a repo whose bytes are refused while its metadata is served is not
    private. (Same drift the four copied effect-command literals produced.)

    `true` is matched case-insensitively and whitespace-trimmed: the tag is written by whichever
    client announced the repo, not by us, so its exact spelling is not ours to assume.
    """
    if not isinstance(event, dict):
        return False
    for t in event.get("tags") or []:
        if (isinstance(t, list) and len(t) >= 2 and t[0] == "private"
                and str(t[1]).strip().lower() == "true"):
            return True
    return False


# --------------------------------------------------------------------------- NIP-34 role tags
#
# THE RECURSIVE MAINTAINER SET IS DEFINED BY AN IMPLEMENTATION, NOT BY A SPEC, AND THIS IS THAT
# IMPLEMENTATION READ AND TRANSCRIBED.
#
# GRASP-01 says a server "MUST accept pushes ... respecting the recursive maintainer set" and defines
# the term nowhere; NIP-34 defines only a flat `maintainers` tag. ngit v3 is where the real rule
# lives, so the functions below are a transcription of ngit-cli 3.0.0 (cloned and read, not guessed):
#
#   src/lib/repo_ref.rs:492  role_entry_is_active
#   src/lib/repo_ref.rs:243  role_boundaries
#   src/lib/repo_ref.rs:556  active_maintainer_projection
#   src/lib/repo_ref.rs:611  announcement_author_declines_maintainership
#   src/lib/client.rs:2121   get_repo_ref_from_cache_with_selected_recovery  (the discovery loop)
#
# A ROLE TAG is ["M"|"m"|"o", <pubkey>, <boundary>...] -- `M` lead, `m` co-maintainer, `o` moderator
# -- whose boundaries are unix timestamps alternating start, end, start, end. An entry is ACTIVE when
# it carries NO boundaries (active from the beginning) or an ODD number of them (an interval opened
# and never closed). `defer` is accepted ONLY as the final value in an end position; any other
# non-numeric boundary makes the record invalid, and an invalid record grants nothing.
#
# Moderators (`o`) are deliberately NOT maintainers: per NIP-34 they can never reach the state-event
# authority checks. They are still WALKED, because their own announcement is where their
# acknowledgement or departure is recorded.

_ROLE_TAGS = ("M", "m", "o")


def _role_entry_active(tag) -> bool:
    """ngit `role_entry_is_active` + `role_boundaries`: no boundaries, or an odd (unclosed) number of
    them, with `defer` legal only as the last value in an end position. Invalid -> not active, which
    is the fail-closed direction: an unparseable role record must not grant authority."""
    raw = list(tag[2:])
    for i, value in enumerate(raw):
        if value == "defer":
            if not (i % 2 == 1 and i + 1 == len(raw)):
                return False                      # `defer` anywhere else invalidates the record
            continue
        try:
            int(value)
        except (TypeError, ValueError):
            return False                          # a non-numeric boundary invalidates the record
    return len(raw) == 0 or len(raw) % 2 == 1


def announcement_roles(event) -> tuple:
    """(maintainers, moderators) named by a 30617's ACTIVE role tags, as hex sets.

    When ANY role tag is present the deprecated `maintainers` tag is IGNORED (ngit
    `repo_ref.rs:73-76`) -- an announcement carrying both is speaking the new language, and merging
    the two would resurrect a maintainer whose role entry has been closed."""
    maints, mods, saw_role = set(), set(), False
    for tag in event.get("tags") or []:
        if not (isinstance(tag, list) and len(tag) >= 2 and tag[0] in _ROLE_TAGS):
            continue
        saw_role = True
        if not _role_entry_active(tag):
            continue
        h = _norm_hex(tag[1])
        if not h:
            continue
        (mods if tag[0] == "o" else maints).add(h)
    if not saw_role:
        for tag in event.get("tags") or []:
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "maintainers":
                for pk in tag[1:]:                # NIP-34 packs multiple pubkeys in one tag
                    h = _norm_hex(pk)
                    if h:
                        maints.add(h)
    return maints, mods


def author_declines_maintainership(event) -> bool:
    """ngit `announcement_author_declines_maintainership`: the author's OWN announcement outranks
    anybody else's assignment of them. True when at least one role tag names the author and none of
    those is an ACTIVE `M`/`m` entry -- they closed their self-role (left) or acknowledged only
    moderatorship.

    An author named by NO role tag has NOT declined: they are implicitly a maintainer for the
    repository's whole history, which is what keeps a legacy `maintainers`-tag repo working.

    This is the only rule in the walk that REMOVES authority, so it is also the only one whose
    absence is a security bug rather than an inconvenience: without it, somebody who resigned keeps
    push access for ever."""
    author = str(event.get("pubkey", ""))
    has_entry = has_active_maint = False
    for tag in event.get("tags") or []:
        if not (isinstance(tag, list) and len(tag) >= 2 and tag[0] in _ROLE_TAGS):
            continue
        if _norm_hex(tag[1]) != author:
            continue
        has_entry = True
        if tag[0] != "o" and _role_entry_active(tag):
            has_active_maint = True
    return has_entry and not has_active_maint


def announcement_urls(event, tag_name: str) -> list:
    """Every value of a repeated 30617 tag (`clone` or `relays`), which NIP-34 allows to be packed
    several-per-tag as well as repeated. Used to answer "does this announcement name THIS service?",
    which GRASP-01 makes the condition of accepting a repository."""
    out = []
    for tag in event.get("tags") or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == tag_name:
            for v in tag[1:]:
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
    return out


# --------------------------------------------------------------------------- Postgres reads
# One indexed query each; no scans (see the JOIN on event_tags(tag,value) + events(kind,pubkey)).

#: How many times the maintainer walk may expand. GRASP-01 says "recursive" and supplies NO bound;
#: neither does NIP-34, and ngit's own loop simply runs to a fixpoint over a LOCAL cache where the
#: cost is zero. Here every round is a Postgres read per newly discovered pubkey, reached from an
#: UNAUTHENTICATED clone, so it needs a ceiling. 6 is a delegation chain six deep -- far past anything
#: a real project has -- and the walk stops early at its own fixpoint, which is the normal case.
_MAINTAINER_MAX_ROUNDS = 6
_MAINTAINER_MAX_PUBKEYS = 64        # and a hard cap on the set, so one hostile announcement listing
#                                     thousands of pubkeys cannot turn a clone into thousands of reads


def load_announcement(conn, pubkey_hex: str, repo_id: str):
    """The newest VALID kind-30617 that `pubkey_hex` signed for `repo_id`, or None.

    NIP-01 addressable-event rules: only an author's latest announcement speaks for them (ngit
    reduces to `latest_announcement_per_author` for exactly this reason -- a stale version can keep an
    ended role active or hide a departure). The signature is re-verified here rather than trusting the
    row, and the author is re-checked, so a poisoned `events` row cannot speak for anybody."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.raw FROM events e "
            "JOIN event_tags t ON t.event_id = e.id AND t.tag = 'd' AND t.value = %s "
            "WHERE e.kind = %s AND e.pubkey = %s "
            "ORDER BY e.created_at DESC LIMIT 4",
            (repo_id, ANNOUNCE_KIND, pubkey_hex))
        rows = cur.fetchall()
    for row in rows:
        try:
            ev = json.loads(row[0])
        except (ValueError, TypeError):
            continue
        if ev.get("pubkey") != pubkey_hex:     # belt-and-suspenders: the row must be this author's
            continue
        if not verify_event(ev):               # re-verify -- never trust the DB row's validity
            continue
        return ev                              # newest VALID announcement by this author wins
    return None


def load_maintainers(conn, owner_hex: str, repo_id: str) -> set:
    """THE maintainer ACL for `<repo_id>` rooted at `owner_hex` -- the RECURSIVE set GRASP-01 requires.

    Transcribed from ngit-cli 3.0.0 `src/lib/client.rs:2121`
    (`get_repo_ref_from_cache_with_selected_recovery`), because GRASP-01 says "respecting the
    recursive maintainer set" and defines it nowhere, and NIP-34 defines only a flat tag. The walk:

      1. seed: the URL owner is both DISCOVERED and a MAINTAINER;
      2. for every discovered pubkey, read their own latest 30617 for this same identifier;
      3. ONLY an announcement whose author is ALREADY IN `maintainers` expands anything -- its
         maintainers join both sets, its moderators join `discovered` alone (a moderator assigns
         nobody; their announcement is consulted solely for their own self-entries);
      4. repeat to a fixpoint;
      5. then drop every author whose OWN latest announcement declines maintainership -- a closed
         self-role, or an acknowledgement of moderatorship only.

    RULE 3 IS THE WHOLE SECURITY ARGUMENT, and it is what the old one-level version was protecting by
    reading `WHERE pubkey = owner` only. Recursion does not weaken it: a stranger's 30617 at a
    DIFFERENT coordinate still injects nothing, because their announcement is only ever consulted
    after somebody already trusted named them. Authority flows outward from the owner and can never
    flow inward.

    ONE DELIBERATE DIVERGENCE FROM ngit: the owner is kept unconditionally, so step 5 cannot remove
    them. ngit is resolving "who speaks for this repository" across the network; we are answering
    "who may write into THIS directory", and the npub in the URL is the directory. Letting an
    announcement lock the owner out of their own path would leave a repo on disk that nobody can push
    to and no recovery short of an operator with a shell.

    Bounded by `_MAINTAINER_MAX_ROUNDS`/`_MAINTAINER_MAX_PUBKEYS`: this runs from an unauthenticated
    clone and the spec supplies no ceiling. Fail-closed as before -- a read that raises propagates to
    a caller that denies."""
    maintainers = {owner_hex}
    discovered = {owner_hex}
    announcements = {}
    for _round in range(_MAINTAINER_MAX_ROUNDS):
        pending = [pk for pk in discovered if pk not in announcements]
        if not pending:
            break                                   # fixpoint: nothing new to read
        for pk in pending:
            announcements[pk] = load_announcement(conn, pk, repo_id)
        grew = False
        for pk, ev in announcements.items():
            if ev is None or pk not in maintainers:
                continue                            # only a MAINTAINER's announcement assigns roles
            if author_declines_maintainership(ev):
                # ...and an author who says they have LEFT does not get to appoint anybody on their
                # way out. ngit's candidate set (`ordered_maintainers`) drops only the departing
                # author and keeps whoever they had listed; its stricter AUTHORITY set
                # (`confirmed_maintainers`) drops those too, because an unconfirmed invitee of a
                # departed member has nobody confirmed vouching for them. This is that one rule
                # taken across, and only that one: resignation must not leave a permanent back door,
                # and it costs nothing on a legacy repo, where nobody declines at all.
                continue
            new_maints, mods = announcement_roles(ev)
            for h in new_maints | mods:
                if h not in discovered and len(discovered) < _MAINTAINER_MAX_PUBKEYS:
                    discovered.add(h)
                    grew = True
            for h in new_maints:
                if h in discovered and h not in maintainers:
                    maintainers.add(h)
                    grew = True
        if not grew:
            break
    # Step 5. A member's own announcement outranks anybody's assignment of them, so somebody who
    # resigned stops being able to push. The owner is exempt (see above).
    for pk in list(maintainers):
        ev = announcements.get(pk)
        if pk != owner_hex and ev is not None and author_declines_maintainership(ev):
            maintainers.discard(pk)
    return maintainers


def load_announced_private(conn, owner_hex: str, repo_id: str) -> bool:
    """GRASP-08: does the OWNER's newest valid kind-30617 for <repo_id> carry ["private","true"]?

    Same ACL reasoning and the same one indexed read as load_maintainers: ONLY `pubkey = owner` is
    considered, so a forged 30617 from another key (which addresses a different coordinate) can
    neither reveal a private repo nor conceal a public one, and the announcement's signature is
    re-verified here rather than trusting the row.

    RAISES on a database error instead of answering. The CALLER decides what "could not ask" means,
    and on the read gate it means deny — returning False here would let an unreachable database
    quietly publish a private repository, which is the failure this function exists to prevent.

    Asks about the OWNER's announcement only, deliberately not the recursive set: privacy here is a
    property of the repository this node hosts at this path, and the owner is who that path belongs
    to. (GRASP-08 defines privacy over the recursive set for a CLIENT deciding where to publish; a
    co-maintainer flipping their own copy private must not take this host's repo off its listing.)
    """
    ev = load_announcement(conn, owner_hex, repo_id)
    return event_says_private(ev) if ev is not None else False


def load_state_events(conn, owner_hex: str, repo_id: str, maintainers) -> list:
    """Candidate kind-30618 events for 30618:<owner>:<repo_id> authored by any maintainer, newest
    first. The SQL pre-filters to maintainers (pubkey = ANY); select_authorized_state then re-verifies
    sigs + picks the newest. LIMIT keeps it to one cheap indexed read (no scan)."""
    mlist = list(maintainers)
    if not mlist:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.raw FROM events e "
            "JOIN event_tags t ON t.event_id = e.id AND t.tag = 'd' AND t.value = %s "
            "WHERE e.kind = %s AND e.pubkey = ANY(%s) "
            "ORDER BY e.created_at DESC LIMIT 8",
            (repo_id, STATE_KIND, mlist))
        rows = cur.fetchall()
    out = []
    for row in rows:
        try:
            out.append(json.loads(row[0]))
        except (ValueError, TypeError):
            continue
    return out
