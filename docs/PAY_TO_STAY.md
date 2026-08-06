# Pay to stay — a paid retention tier for the relay

An **optional** feature, **off on every node** until an admin turns it on in **Admin → Nostr Relay →
Pay to stay**. With it off, nothing in this document happens and the relay's auto-clean behaves
exactly as it always has.

## What problem it solves

Publishing to this relay is web-of-trust gated, but the WoT is large — and **everything a client
publishes here is stored forever**. The prune's age rules have always excluded `origin='direct'`
events (data somebody entrusted to us) and every author in the preserve set. That is a storage bill
that grows without limit and that nobody is paying for.

Pay-to-stay puts a window on *that* content, and lets an author buy a longer one:

| | kept for |
|---|---|
| **Free** — an author with no account here and no subscription | `nostr_relay_free_retention_days` |
| **Paid** — an author with a live subscription | `nostr_relay_paid_retention_days` (`0` = forever) |
| **Your users** — accounts here, NIP-05 holders, operators, bridged puppets | forever, always |

Only high-volume **feed** kinds are ever aged out (notes, reposts, reactions, comments, public chat,
articles, live events — `store._PRUNABLE_KINDS`). Profiles, contact lists, relay lists, DMs, git
events and the app's own kind-30078 datastore are never touched, for anyone, at any age.

## How someone pays

They **zap the relay's profile** from a Nostr client. The zap receipt (kind 9735) is picked up by
`paid_retention_service`, verified, and converted to days at `nostr_relay_paid_sats_per_month`.
Part-payments carry over, so small zaps accumulate instead of rounding to nothing, and renewing
early adds to the time already bought rather than restarting from today.

**Why a zap and not a payment.** A Lightning payment carries no identity — there would be nobody to
credit. A zap does: the receipt embeds the payer's signed kind-9734 request. That is also why the
QR on the relay's splash page encodes the `nostr:` **profile**, never a `lightning:` URI, and why a
zap of a *post* stays an ordinary tip (the operator can still be tipped for a post without it
silently becoming a storage purchase).

**What makes a receipt trustworthy** is *not* that it exists on our relay — any WoT member can
publish a kind 9735 claiming anything. It is that the receipt is signed by the **zapper service of
our own lightning address**: NIP-57 has the LNURL-pay endpoint publish a `nostrPubkey`, and only
that key's signature means an invoice was actually paid. `verify_receipt()` checks, in order:

1. kind 9735, signature valid, author == the `nostrPubkey` resolved from `nostr_relay_paid_lud16`;
2. the embedded kind-9734 request parses, and **its** signature is valid (so a payer can't be
   impersonated);
3. both the receipt and the request are `p`-tagged to the configured receiving pubkey;
4. the request has no `e` tag (profile zap, not a post tip);
5. the amount comes from the **bolt11 invoice**. If an invoice is present but unreadable the receipt
   is refused rather than falling back to the request's `amount` tag — that tag is the one number in
   a receipt the payer controls.

## Where the state lives

One operator-signed, NIP-44-encrypted kind-30078 document, `d=pcai:kv:paid_retention`, in the
relay — not a SQL table. It holds `{subs: {pubkey: {until, msats, bal, since}}, cursor, credited}`.
The scan cursor starts at *now* when the ledger is created, so enabling the feature never
retroactively credits historical tips.

Three processes touch it, and the split is deliberate:

* the **worker** is the sole writer (`scan_once`, every 5 min);
* the **relay** reads it before each prune (`thread._refresh_subscribers`);
* the **app** reads it for `GET /client/retention` and the admin panel.

## The two ways this can lose somebody something, and what stops each

**A wiped ledger.** The document is replaceable, so writing after a failed read replaces every
subscription with nothing — the same failure that once took out a drive's file index. Every read is
`strict=True`, and a read that failed is never written back.

**A prune that can't tell "nobody paid" from "I couldn't ask".** That one deletes exactly the notes
people paid to keep. `store.set_subscribers(pubkeys, ledger_ok=…)` carries the distinction, and the
tiered rules are skipped for the whole pass when `ledger_ok` is False — including when the ledger
document does not exist at all. A skipped prune costs disk; the alternative costs data.

Both, plus the four ways the tier must refuse to delete (feature off, `free_days=0`, unreadable
ledger, author with an account here), are pinned in `tests/test_paid_retention.py`.

## Settings (Admin → Nostr Relay)

| key | default | meaning |
|---|---|---|
| `nostr_relay_paid_retention_enabled` | off | master switch; off = nothing below has any effect |
| `nostr_relay_free_retention_days` | `0` | **the setting that deletes things**; `0` = keep forever |
| `nostr_relay_paid_retention_days` | `0` | subscriber window; `0` = forever while subscribed |
| `nostr_relay_paid_sats_per_month` | `0` | price; `0` = nothing can be bought |
| `nostr_relay_paid_lud16` | — | lightning address whose zapper service signs receipts |
| `nostr_relay_paid_pubkey` | — | profile people zap; blank = NIP-11 admin pubkey, else operator key |

Enabling the feature deletes nothing on its own — the free window is a separate, explicit number.
Turn it on, set a price and address, then **Preview auto-clean** before typing a free window; the
preview breaks out `aged_free` / `aged_paid` and the counts are the ones the real prune uses.

Changing any of the three prune-affecting settings reloads the running relay live (no restart), the
same path `retention_days` takes.

## What visitors see

* the relay's **splash page** (`/relay` in a browser) grows a "How long your posts are kept" section
  and, when a price is set, a scannable profile QR;
* **NIP-11** gains `retention` and `fees.subscription`, so a client can warn a user before their
  notes age out;
* `GET /client/retention?pubkey=<hex>` returns the policy plus that author's standing
  (`known: false` means the ledger couldn't be read — never render that as "not subscribed").

## Ops

Admin → Nostr Relay → **Subscribers** lists the ledger and grants days by hand (negative days take
them back) — for a payment that arrived another way, a comp, or undoing a bad credit. A payer gets a
NIP-17 DM confirming the extension.

When a subscription lapses the free window applies again, so a lapsed author's older posts go on the
next auto-clean. There is no separate grace period.

## Dependencies

None new. It uses `httpx`, `segno`, `apscheduler` and `websockets`, all already in both
`requirements.txt` and `requirements-nostr.txt` (the profile a relay-only node installs).
