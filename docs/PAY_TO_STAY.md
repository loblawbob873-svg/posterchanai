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

## How it sits next to the auto-clean you already have

The pre-existing rules are unchanged, and the two sets are **disjoint by origin**: every old rule
carries `origin != 'direct'`, both new ones carry `origin = 'direct'`. Turning the feature off
restores today's behaviour exactly, because there is no shared predicate to get wrong.

| rule | what it deletes | window | new? |
|---|---|---|---|
| NIP-40 expiration sweep | anything with an `expiration` tag (except git + 30078) | author's own | no |
| age prune | **synced** firehose feed content | `nostr_relay_retention_days` | no |
| bridge DM TTL | puppet-addressed gift wraps | 4 days, fixed | no |
| count cap | synced feed content | `nostr_relay_max_events` | no |
| pay-to-stay free | direct writes, unsubscribed, no account here | `free_retention_days` | yes |
| pay-to-stay paid | direct writes, subscribed | `paid_retention_days` | yes |

The nightly job and the Admin **Run auto-clean now** button are one code path (`_prune_fresh`), which
re-reads the tier windows and the subscriber ledger before pruning — as does the dry-run preview, so
the counts on the button are the counts the real run uses.

**A subscriber is also exempt from the two OLD rules** (`store._subscriber_exempt`), for the age
prune and the count cap. Otherwise "your posts stay" would quietly mean "unless the copy we hold
arrived over the firehose", which is our implementation detail, not something they bought a
different answer for. The block purge (words/languages/pubkeys) is deliberately **not** exempt —
that is moderation, and paying does not buy immunity from it.

That exemption handles a failed ledger read **differently from the tiered rules, on purpose**:

* a direct write can be the only copy in existence, so an unreadable ledger disables the tiered
  rules entirely — the loss would be unrecoverable;
* a synced row is a mirror of a note that still lives on the relays it came from, and the rule it
  belongs to is the relay's only bound on firehose growth. Skipping it on a hiccup would trade a
  recoverable loss for unbounded disk, so it keeps running and falls back to the last subscriber set
  that was successfully read. Over-protecting a mirror is harmless, and the set still shrinks the
  moment a real read shows a subscription lapsed. Turning the master switch off drops the remembered
  set, so a stale copy can't go on exempting anyone.

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

## Who gets told what

All DMs are NIP-17, sent from the node's **system identity** via `system_dm` — never the operator
key, which on a single-admin node is the admin's own, making it a self-DM that files under
note-to-self with no unread count. Same reasoning as uptime alerts and access grants.

| event | who | why |
|---|---|---|
| payment credited | payer | confirms the zap landed and states the new expiry |
| payment too small for a day | payer | they paid and got no days — silence reads as "my money vanished", so it says what was banked and what's needed |
| any payment | **admin** — Nostr DM, plus Telegram if the admin has one linked | money arriving shouldn't need going to look for |
| admin grant | recipient | the one interaction whose answer is "yes" shouldn't be discovered by trying again |
| **7 days before expiry** | subscriber | **the one that prevents a loss** — after the lapse the free window applies and the next auto-clean takes their older posts |
| expiry | subscriber | says exactly what happens now, and how to start again |

The last two live in `notify_lifecycle()`, which runs every tick regardless of price or lightning
address, because an admin-granted subscription expires exactly like a bought one. Three details that
are load-bearing:

* the warned/ended markers are keyed on the **expiry timestamp**, so renewing re-arms both with no
  extra bookkeeping — and `_normalize` carries them deliberately, because dropping them on the next
  read would re-send both DMs every five minutes;
* the marker is written **only for a DM that actually went out** (plan under the lock → send →
  re-read and mark). Marking first is safe against repeats but lets one transient publish failure
  swallow a subscriber's only warning before their posts are deleted. A repeat is an annoyance; a
  miss is the failure this exists to prevent. The re-read also skips a record whose expiry changed
  mid-flight, so a renewal landing between the two phases doesn't inherit a stale marker;
* an ending older than the warning window is marked but **not announced** — the tick catches a real
  one within minutes, so anything older is a ledger predating this code or a worker that was down,
  and a backlog of "your subscription ended" arriving at once reads as a malfunction;
* DMs are sent outside the write lock, so a failed DM can't roll back a payment.

The **payment** path is the deliberate opposite: there nothing is sent unless the ledger write
succeeded. The asymmetry is principled — a payment DM asserts persisted state (announcing a credit
that wasn't saved is a false promise, and the unsaved dedup id means the next scan re-credits it
anyway), while a lapse warning asserts a fact about the clock, true whether or not the marker saved.

There is no grace period: when a subscription lapses the free window applies again and the lapsed
author's older posts go on the next auto-clean. The 7-day warning is what makes that fair.

## Ops

Admin → Nostr Relay → **Subscribers** lists the ledger and grants days by hand (negative days take
them back) — for a payment that arrived another way, a comp, or undoing a bad credit.

## Dependencies

None new. It uses `httpx`, `segno`, `apscheduler` and `websockets`, all already in both
`requirements.txt` and `requirements-nostr.txt` (the profile a relay-only node installs).
