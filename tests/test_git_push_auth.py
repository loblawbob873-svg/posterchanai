#!/usr/bin/env python3
"""Direct unit tests of the GRASP push-auth decision (app/services/git_auth) with CRAFTED, really
signed Nostr events. This is the security core — run it and read every line.

Cases (push auth):
  (a) authorized maintainer + matching 30618            -> ACCEPT
  (b) valid sig but pubkey NOT in the maintainer ACL    -> REJECT
  (c) tampered 30618 (bad sig)                          -> REJECT
  (d) stale 30618 (older created_at) vs a newer one     -> uses the NEWER
  (e) ref SHA mismatch vs signed state                  -> REJECT
  (f) no 30618 present                                  -> REJECT
Plus: delete authorization, force-push gating, and NIP-98 read-gate (private repos).

Exit code is non-zero if ANY case fails, so CI/the reviewer gets a hard signal.
"""

import base64
import json
import os
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import git_auth
from app.services.nostr import bip340
from app.services.nostr.event import build_event

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
REF = "refs/heads/main"
REPO_ID = "demo"

# Deterministic test keys.
OWNER_SK = (11).to_bytes(32, "big")
MAINT_SK = (22).to_bytes(32, "big")
RANDO_SK = (33).to_bytes(32, "big")
OWNER = bip340.pubkey_from_seckey(OWNER_SK).hex()
MAINT = bip340.pubkey_from_seckey(MAINT_SK).hex()
RANDO = bip340.pubkey_from_seckey(RANDO_SK).hex()

MAINTAINERS = {OWNER, MAINT}   # owner ∪ 30617.maintainers (as load_maintainers would return)

_results = []


def check(name, condition):
    ok = bool(condition)
    _results.append(ok)
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))


def state_event(sk, ref_sha_map, *, created_at=None, repo_id=REPO_ID):
    """A real, signed kind-30618 with the given refs/<b>->sha tags."""
    tags = [["d", repo_id]]
    for ref, sha in ref_sha_map.items():
        tags.append([ref, sha])
    return build_event(sk, 30618, "", tags=tags, created_at=created_at)


def main():
    now = int(time.time())
    print("keys: owner=%s… maint=%s… rando=%s…" % (OWNER[:8], MAINT[:8], RANDO[:8]))

    # (a) authorized maintainer + matching 30618 -> ACCEPT
    ev_a = state_event(OWNER_SK, {REF: SHA_B}, created_at=now)
    ok, reason = git_auth.decide_push_ref(REF, SHA_A, SHA_B, MAINTAINERS, [ev_a])
    print("(a) accept authorized+matching -> %s (%s)" % (ok, reason))
    check("a: authorized maintainer + matching 30618 ACCEPTS", ok is True)

    # (b) valid sig but signer NOT in the maintainer ACL -> REJECT
    ev_b = state_event(RANDO_SK, {REF: SHA_B}, created_at=now)   # perfectly valid, wrong signer
    ok, reason = git_auth.decide_push_ref(REF, SHA_A, SHA_B, MAINTAINERS, [ev_b])
    print("(b) reject non-maintainer signer -> %s (%s)" % (ok, reason))
    check("b: valid sig, signer not in ACL REJECTS", ok is False)

    # (c) tampered 30618 (bad signature) -> REJECT
    ev_c = state_event(OWNER_SK, {REF: SHA_B}, created_at=now)
    ev_c = dict(ev_c)
    ev_c["tags"] = [["d", REPO_ID], [REF, SHA_B], ["injected", "evil"]]   # id/sig no longer match
    ok, reason = git_auth.decide_push_ref(REF, SHA_A, SHA_B, MAINTAINERS, [ev_c])
    print("(c) reject tampered/bad-sig -> %s (%s)" % (ok, reason))
    check("c: tampered 30618 (bad sig) REJECTS", ok is False)

    # (d) stale vs newer: older says SHA_A, newer says SHA_B -> the NEWER wins.
    ev_old = state_event(MAINT_SK, {REF: SHA_A}, created_at=now - 1000)
    ev_new = state_event(OWNER_SK, {REF: SHA_B}, created_at=now)
    ok_new, r_new = git_auth.decide_push_ref(REF, SHA_A, SHA_B, MAINTAINERS, [ev_old, ev_new])
    ok_stale, r_stale = git_auth.decide_push_ref(REF, SHA_A, SHA_A, MAINTAINERS, [ev_old, ev_new])
    print("(d) push newer SHA_B -> %s ; push stale SHA_A -> %s" % (ok_new, ok_stale))
    check("d: newest-by-created_at is used (SHA_B accepts)", ok_new is True)
    check("d: stale state does NOT authorize (SHA_A rejects)", ok_stale is False)
    # order independence: shuffle input
    ok_shuf, _ = git_auth.decide_push_ref(REF, SHA_A, SHA_B, MAINTAINERS, [ev_new, ev_old])
    check("d: selection is order-independent", ok_shuf is True)

    # (e) ref SHA mismatch: signed says SHA_B, push tries SHA_C -> REJECT
    ev_e = state_event(OWNER_SK, {REF: SHA_B}, created_at=now)
    ok, reason = git_auth.decide_push_ref(REF, SHA_A, SHA_C, MAINTAINERS, [ev_e])
    print("(e) reject SHA mismatch -> %s (%s)" % (ok, reason))
    check("e: newsha != signed sha REJECTS", ok is False)

    # (f) no 30618 present -> REJECT
    ok, reason = git_auth.decide_push_ref(REF, SHA_A, SHA_B, MAINTAINERS, [])
    print("(f) reject no-state -> %s (%s)" % (ok, reason))
    check("f: no signed 30618 REJECTS", ok is False)

    # --- extra invariants ---------------------------------------------------
    # delete: allowed only if the signed state also drops the ref.
    ev_nodrop = state_event(OWNER_SK, {REF: SHA_B}, created_at=now)     # still pins the ref
    ev_drop = state_event(OWNER_SK, {"refs/heads/other": SHA_B}, created_at=now)  # ref absent
    ok_bad_del, _ = git_auth.decide_push_ref(REF, SHA_B, git_auth.ZERO_SHA, MAINTAINERS, [ev_nodrop])
    ok_ok_del, _ = git_auth.decide_push_ref(REF, SHA_B, git_auth.ZERO_SHA, MAINTAINERS, [ev_drop])
    check("delete rejected while state still pins the ref", ok_bad_del is False)
    check("delete accepted when state drops the ref", ok_ok_del is True)

    # force-push gating: matching SHA but non-fast-forward.
    ev_f = state_event(OWNER_SK, {REF: SHA_B}, created_at=now)
    ok_force_on, _ = git_auth.decide_push_ref(REF, SHA_A, SHA_B, MAINTAINERS, [ev_f],
                                              is_non_fast_forward=True, allow_force=True)
    ok_force_off, _ = git_auth.decide_push_ref(REF, SHA_A, SHA_B, MAINTAINERS, [ev_f],
                                               is_non_fast_forward=True, allow_force=False)
    check("force-push accepted when allow_force on (signed)", ok_force_on is True)
    check("force-push rejected when allow_force off", ok_force_off is False)

    # NIP-98 convenience path: maintainer signer bypasses (auto-derive in post-receive).
    ok_n98, _ = git_auth.decide_push_ref(REF, SHA_A, SHA_C, MAINTAINERS, [], nip98_signer=MAINT)
    ok_n98_bad, _ = git_auth.decide_push_ref(REF, SHA_A, SHA_C, MAINTAINERS, [], nip98_signer=RANDO)
    check("NIP-98 maintainer signer authorizes push", ok_n98 is True)
    check("NIP-98 non-maintainer signer does NOT authorize", ok_n98_bad is False)

    # forged-30617 scenario: a rando's 30618 addressing the SAME repo id is simply not in the ACL,
    # so it can't self-authorize (mirrors load_maintainers reading only 30617:<owner>:<id>).
    ev_forge = state_event(RANDO_SK, {REF: SHA_B}, created_at=now + 5000)  # newest, but wrong signer
    ok_forge, _ = git_auth.decide_push_ref(REF, SHA_A, SHA_B, MAINTAINERS, [ev_forge, state_event(OWNER_SK, {REF: SHA_A}, created_at=now)])
    check("forged newer 30618 from non-maintainer is ignored (uses owner's, mismatch REJECTS)", ok_forge is False)

    # --- NIP-98 read gate (private repos) -----------------------------------
    def nip98_header(sk, method, url, created_at=None):
        ev = build_event(sk, 27235, "",
                         tags=[["u", url], ["method", method]],
                         created_at=created_at if created_at is not None else int(time.time()))
        return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()

    url = "https://poster.place/git/npub1xxx/%s.git/info/refs?service=git-upload-pack" % REPO_ID
    needle = "%s.git" % REPO_ID
    readers = {OWNER, MAINT}
    # (read-a) allowlisted reader -> granted
    h_ok = nip98_header(MAINT_SK, "GET", url)
    signer = git_auth.verify_nip98(h_ok, None, needle, readers, require_method=False, max_skew=300)
    check("read-gate: allowlisted reader NIP-98 GRANTS", signer == MAINT)
    # (read-b) non-allowlisted signer -> denied
    h_bad = nip98_header(RANDO_SK, "GET", url)
    signer = git_auth.verify_nip98(h_bad, None, needle, readers, require_method=False, max_skew=300)
    check("read-gate: non-allowlisted signer DENIED", signer is None)
    # (read-c) stale header -> denied
    h_stale = nip98_header(MAINT_SK, "GET", url, created_at=int(time.time()) - 100000)
    signer = git_auth.verify_nip98(h_stale, None, needle, readers, require_method=False, max_skew=300)
    check("read-gate: stale NIP-98 DENIED", signer is None)
    # (read-d) header bound to a DIFFERENT repo -> denied (cross-repo replay)
    h_other = nip98_header(MAINT_SK, "GET", "https://poster.place/git/npub1xxx/other.git/info/refs")
    signer = git_auth.verify_nip98(h_other, None, needle, readers, require_method=False, max_skew=300)
    check("read-gate: header bound to another repo DENIED", signer is None)
    # (read-e) tampered header (bad sig) -> denied
    ev = build_event(MAINT_SK, 27235, "", tags=[["u", url], ["method", "GET"]])
    ev["tags"].append(["x", "tamper"])   # invalidates id/sig
    h_tamper = "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()
    signer = git_auth.verify_nip98(h_tamper, None, needle, readers, require_method=False, max_skew=300)
    check("read-gate: tampered NIP-98 (bad sig) DENIED", signer is None)

    # --- Basic-envelope NIP-98 (ngit / libgit2 cannot send `Authorization: Nostr`) ---------------
    # Same signed token, carried as the password half of HTTP Basic. Every other check must still
    # apply — this is an envelope, not a second way in.
    def basic_header(sk, method, url, created_at=None, user="npub1xxx"):
        raw = nip98_header(sk, method, url, created_at).split(" ", 1)[1]
        return "Basic " + base64.b64encode(("%s:%s" % (user, raw)).encode()).decode()

    b_ok = basic_header(MAINT_SK, "GET", url)
    signer = git_auth.verify_nip98(b_ok, None, needle, readers, require_method=False, max_skew=300,
                                   allow_basic=True)
    check("basic-envelope: allowlisted reader GRANTS", signer == MAINT)
    # opt-in only: the push path never passes allow_basic, so the same header must be refused there
    signer = git_auth.verify_nip98(b_ok, None, needle, readers, require_method=False, max_skew=300)
    check("basic-envelope: REFUSED when allow_basic not set (push path unchanged)", signer is None)
    # the wrapped token is still fully checked
    signer = git_auth.verify_nip98(basic_header(RANDO_SK, "GET", url), None, needle, readers,
                                   require_method=False, max_skew=300, allow_basic=True)
    check("basic-envelope: non-allowlisted signer DENIED", signer is None)
    signer = git_auth.verify_nip98(basic_header(MAINT_SK, "GET", url, created_at=int(time.time()) - 100000),
                                   None, needle, readers, require_method=False, max_skew=300, allow_basic=True)
    check("basic-envelope: stale token DENIED", signer is None)
    signer = git_auth.verify_nip98(basic_header(MAINT_SK, "GET", "https://poster.place/git/npub1xxx/other.git/info/refs"),
                                   None, needle, readers, require_method=False, max_skew=300, allow_basic=True)
    check("basic-envelope: token bound to another repo DENIED", signer is None)
    # a REAL password (not a NIP-98 event) must never authenticate
    pw = "Basic " + base64.b64encode(b"npub1xxx:hunter2").decode()
    signer = git_auth.verify_nip98(pw, None, needle, readers, require_method=False, max_skew=300,
                                   allow_basic=True)
    check("basic-envelope: ordinary password DENIED (no HTTP passwords)", signer is None)
    # malformed Basic payloads must not raise
    for bad in ("Basic " + base64.b64encode(b"nocolon").decode(), "Basic !!!not-base64!!!", "Basic "):
        signer = git_auth.verify_nip98(bad, None, needle, readers, require_method=False,
                                       max_skew=300, allow_basic=True)
        check("basic-envelope: malformed payload DENIED (%r)" % bad[:24], signer is None)

    passed = sum(_results)
    total = len(_results)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
