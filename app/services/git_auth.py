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
      - created_at within ±max_skew of now (the replay window);
      - the signer pubkey is in `allowed`.

    require_method=False is used for the READ gate: a `git clone` sends the SAME static
    `http.extraHeader` for both the info/refs GET and the upload-pack POST, so we can't demand the
    method tag match both — the repo binding + freshness + access-set membership are the guard, over
    TLS. Push keeps require_method=True (writes are higher-stakes).

    allow_basic=True additionally accepts the SAME base64 event carried as the password half of an
    `Authorization: Basic <b64 user:pass>` header, so any client that can only do username/password
    can still present a NIP-98 token — `scripts/git-credential-nostr` mints a fresh one per request.
    Every check below is unchanged, so this is a second envelope for the same signed token, not a
    second way to authenticate. Enabled for the READ gate only — push never sets it.

    This does NOT make ngit work with private repos, which is what it was originally written for.
    ngit 2.6.3 was measured: it sends no NIP-98 AND never invokes a git credential helper (verified
    with a logging helper — it was not called once), so it simply cannot authenticate. The envelope
    is still what lets plain `git clone https://…/<id>.git` read a private repo with no manual
    header wrangling. Private repos reached over `nostr://` need an authenticated git server ngit
    can actually speak to (SSH), not this.

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


# --------------------------------------------------------------------------- Postgres reads
# One indexed query each; no scans (see the JOIN on event_tags(tag,value) + events(kind,pubkey)).

def load_maintainers(conn, owner_hex: str, repo_id: str) -> set:
    """Maintainer ACL for 30617:<owner_hex>:<repo_id>. Reads ONLY the owner's own announcement
    (WHERE pubkey=owner) so a forged 30617 from another pubkey (a different addressable coordinate)
    can't inject maintainers. The announcement's signature is re-verified. Returns owner ∪ maintainers.

    Owner is ALWAYS a maintainer even with no announcement (the URL npub owns the path)."""
    maints = {owner_hex}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.raw, e.created_at FROM events e "
            "JOIN event_tags t ON t.event_id = e.id AND t.tag = 'd' AND t.value = %s "
            "WHERE e.kind = %s AND e.pubkey = %s "
            "ORDER BY e.created_at DESC LIMIT 4",
            (repo_id, ANNOUNCE_KIND, owner_hex))
        rows = cur.fetchall()
    for row in rows:
        raw = row[0]
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if ev.get("pubkey") != owner_hex:      # belt-and-suspenders: only the owner's announcement
            continue
        if not verify_event(ev):               # re-verify — never trust the DB row's validity
            continue
        for tag in ev.get("tags") or []:
            if len(tag) >= 2 and tag[0] == "maintainers":
                for pk in tag[1:]:             # NIP-34 packs multiple pubkeys in one tag
                    h = _norm_hex(pk)
                    if h:
                        maints.add(h)
        break                                  # newest VALID owner-signed announcement wins
    return maints


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
