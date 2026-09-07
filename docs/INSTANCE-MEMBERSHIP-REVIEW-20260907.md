# Instance membership, cleanup, and client regression review

The restricted apps require an approved instance name registered for the authenticated public key
and the same address in that key's verified signed profile. Registration alone does not qualify.
The instance's configured domain is used; a request Host header cannot grant membership.

Restricted views: News, Meme Builder, Email, Documents, Folder Sync, Passwords, Torrents,
My Analytics, Media Center, Git, Texts, Notes, Monero Wallet, Wallet, and Web Search.
Existing app permissions still apply. App membership has no administrator exemption.

## Reviewed behavior

- Server-backed app routes enforce membership after their existing authentication. Jellyfin
  device credentials and playback capabilities retain their protocol and sharing checks.
- Web Search includes the raw search API, tickets, framed pages, assets, and AI summaries.
  Authenticated peer searches retain their existing load-balancer authorization.
- Verified profiles are ordered by timestamp and event ID. Forged profiles, stale responses,
  wrong-account responses, incomplete relay reads, and stale cached decisions cannot grant access.
- Profile edits force a fresh check. Client state is bound to instance, account, and profile.
  Previously verified local Notes, Passwords, Texts, and Analytics remain available during outages;
  that local fallback never authorizes a server API and is invalidated by a profile change or denial.
  Late hydration is distinguished from editing/removing a profile address.
- The generic splash uses instance branding and explains Edit profile → NIP-05 / verified address
  → Save. Qualified profiles suppress it; changing the address makes it eligible to return.
- Cleanup and Preview share one plan. The scheduled cleanup remains every 15 minutes and now checks
  signed profiles rather than preserving all registered names. Verification is bounded and completes
  before any revocation. An outage or concurrent registry change aborts without changing permissions.
  The cleanup's existing infrastructure and configured Fediverse exemptions remain separate from
  app membership. Cleanup does not invent or restore administrator-granted permissions.
- Documents uses the actual authenticated request bridge. Terminal clipboard writes are ordered
  before paste; non-text results cannot enter xterm. Terminal font size is readable and adjustable.
  Auto-mute saves the preference before waiting for its lazily loaded module.

## Live read-only checks

The running old cleanup was enabled and scheduled correctly but preserved registered identities
without checking their profile. At the initial audit, 63 of 97 registered public keys had matching
locally stored signed profiles; 34 had different or missing profiles. No invalid signature was found.

The revised checker accepted all 63 matching profiles in 0.35 seconds. The revised Preview completed
in 25.3 seconds and selected 18 accounts: 18 AI grants, 11 streaming grants, and 18 whitelist entries.
These were previews, not live revocations. Existing exemptions account for some differences between
profile counts and targeted account counts.

The native bundle review also found that Android downloaded production's page while copying
JavaScript from the checkout, omitting newly added scripts during deployment races. Both native
builders now share a local template renderer. An offline build test prevents a network fetch from
returning; all native workflows watch the shared renderer.

## Validation and release

Focused tests exercise signed profiles and real HTTP routes, browser account/profile transitions,
offline reload and hydration, actual Documents callers, delayed clipboard writes in real xterm,
and cancellation/timeout of a real local proxy socket. A private GNU screen/Codex diagnostic confirmed
that drafts reach the PTY once and remain visible through viewport and font-size changes; it submits
no prompt and does not use the operator's terminal session. The reported duplicated prompt itself
was not reproduced.

The final backend rerun passed **7,885 tests**, with 18 environment-dependent skips and 519
subtests. The full client suite passed **3,547 tests**, with one documented skip and 121 subtests.
The other agent's installer checks passed 122 tests and 52 subtests; their commits were merged
without editing their installer files or working documents.

Live verification of the initial release caught a storage-node issue: the NAS has a separate
NIP-05 registry and was rejecting already-qualified frontend viewers. The correction preserves
the frontend's membership decision only for a distinct identity constructed after shared-secret
verification. Direct users still require membership; media permissions, library ACLs, and playback
signatures still apply. Regression cases cover an unavailable NAS registry, forged delegation,
frontend denial before network access, and invalid playback tickets. The backend rerun above
includes this correction.

The live cleanup revoked 18 AI grants, 11 streaming grants, and 18 whitelist entries at
2026-09-07T16:32:31Z. Its saved result persisted; a subsequent preview reported zero outstanding
revocations. The real scheduler fired at 16:44:55Z with a 15-minute interval. That scheduled
attempt aborted safely when an upstream profile relay was unavailable; it changed no permissions
and retained the next scheduled retry. A restricted permissions snapshot was saved before cleanup.
All 63 matching primary-instance signed profiles passed the revised checker.

Android and Windows release packages were extracted and their nine changed client assets compared
against the source (normalizing Windows CRLF). Both load the membership controller before app.js.
Three actual clipboard checks passed against each packaged terminal controller. Android build
2223 and Windows 1.0.1503 contain the client changes; no browser extension code changed.
The Android large-folder device fixture now supplies a verified membership response, while checking
that anonymous access and removing the profile address remain denied. The previous anonymous
fixture correctly hit the new access gate instead of opening Folder Sync.

Installer files, including `os/gentoo.sh`, remain owned by the other agent. This app deployment
does not publish their installer overlay or ISO.
