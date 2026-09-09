"""Who may cause a repository to exist here — ONE policy, read by the gate and by NIP-11.

GRASP-01 requires a service to serve a git repository "for each accepted git repository
announcement", and separately permits rejecting announcements on quota / WoT / whitelist grounds.
That pairing is what makes acceptance a *resource* decision: on a GRASP service, accepting an
announcement means allocating disk, so the acceptance rule is the only thing standing between a
stranger's signed event and a `git init --bare` here.

GRASP-01 also requires the service to PUBLISH that rule, in prose:

    2. MUST list repository acceptance criteria under `repo_acceptance_criteria` as a human
       readable string
                                                    -- grasp.git 01.md, commit f35b4f9a4ed2

Two copies of a rule — one enforced, one advertised — is a promise that goes stale the first time
either moves, and it goes stale SILENTLY: the relay keeps advertising a policy nobody implements
and a client keeps being refused for a reason the document says does not apply. So the setting is
the POLICY, not the sentence, and the sentence is rendered from it. There is nowhere to type a
criteria string that the gate does not honour.

The auto-provisioning gate (the git host) and `nip11_doc` (the relay) both read `SETTING`.
"""
from __future__ import annotations

# The one setting name. The git host's auto-provisioning gate and the relay's NIP-11 document must
# read THIS key and nothing else, or the two halves drift back apart.
SETTING = "git_repo_acceptance"

# The agreed default. It is deliberately not the most permissive option: an open policy is an
# unauthenticated disk-allocation primitive, and the per-repo/total size caps are enforced at PUSH
# time, so an empty-repo flood costs nothing to send and is unbounded.
DEFAULT = "account_or_wot"

# policy -> the human-readable criteria string GRASP-01 requires. Written as a sentence a person
# being refused can act on, since that is the only thing this field is for.
POLICIES: dict[str, str] = {
    "account_or_wot":
        "A repository announcement is accepted, and its repository provisioned, when the announcing "
        "pubkey has an account on this node or is already in this relay's web of trust. Operators "
        "may allow additional pubkeys explicitly.",
    "wot":
        "A repository announcement is accepted, and its repository provisioned, when the announcing "
        "pubkey is in this relay's web of trust. Operators may allow additional pubkeys explicitly.",
    "account":
        "A repository announcement is accepted, and its repository provisioned, only when the "
        "announcing pubkey has an account on this node. Operators may allow additional pubkeys "
        "explicitly.",
    "allowlist":
        "A repository announcement is accepted, and its repository provisioned, only for pubkeys the "
        "operator has explicitly allowed.",
    "open":
        "Any valid repository announcement is accepted and its repository provisioned, subject to "
        "the service's size quotas.",
    "closed":
        "Repositories are not provisioned from announcements on this service. An operator creates "
        "each repository, and only then is its announcement served.",
}


def normalize(raw) -> str:
    """A stored value that names no policy falls back to the DEFAULT rather than to the most
    permissive entry. A typo in a settings box must never widen who may allocate disk here."""
    v = (str(raw or "")).strip().lower().replace("-", "_")
    return v if v in POLICIES else DEFAULT


def criteria_text(raw=None) -> str:
    """The `repo_acceptance_criteria` string for the policy `raw` names."""
    return POLICIES[normalize(raw)]


def accepts(raw, *, has_account: bool, in_wot: bool, allowlisted: bool) -> bool:
    """The gate itself, over facts its caller has already established.

    It takes booleans rather than a pubkey on purpose: "has an account here" is a Postgres read in
    the app process and "is in the web of trust" is a relay-process structure, and this module is
    imported by both. Keeping the DECISION here and the lookups at the call sites is what lets one
    policy govern two processes without either importing the other's world.

    The operator allowlist is additive under every policy except `closed` — it exists to name an
    operator key that is in no social graph, which is exactly the key a node needs to provision
    its own repositories.
    """
    p = normalize(raw)
    if p == "closed":
        return False
    if allowlisted:
        return True
    if p == "open":
        return True
    if p == "allowlist":
        return False
    if p == "wot":
        return in_wot
    if p == "account":
        return has_account
    return has_account or in_wot          # account_or_wot
