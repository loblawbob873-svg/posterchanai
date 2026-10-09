# PosterChanDB — a RAM-first store for the relay's events

Status: **design** (2026-10-09), Python (numpy + stdlib). Nothing here is deployed. Numbers are MEASURED on server1's live relay
with `scripts/posterchandb_measure.py` (a 2% sample, scaled), not estimated from first principles.

## Why

The relay's Postgres database is 15 GB, and 95% of it is the event store:

| | size | note |
|---|---|---|
| `events` (2.64M) | 7.3 GB | 2.3 GB rows, 3.2 GB indexes, 1.8 GB TOAST |
| ↳ `idx_events_content_fts` | 2.4 GB | 1,779 scans, ever (the primary key: 2 billion) |
| `event_tags` (16.2M) | 7.1 GB | primary key alone 3.3 GB — the 64-char hex id repeated in every row |
| everything else | < 1 GB | users, bridge ledger, Blossom ownership … |

Reads are not the problem (0.3–0.4 ms; the database fits in its 20 GB of shared buffers). Size, RAM
and the write policy are: `synchronous_commit` is `off` for the WHOLE database, so a power cut can lose a
person's own post after the relay said OK — the one write that is often the only copy.

## What it holds (measured)

| | per event | 2.64M events |
|---|---|---|
| Postgres today | ~5.7 KB | 15 GB |
| the events as JSON | 1,473 B | 3.9 GB |
| **PosterChanDB record** | **872 B** | **2.3 GB** |
| in-RAM indexes | | **~340 MB** (bitmaps ~265, id map 63, time column 11) |

The record today is 133 B fixed (raw id/pubkey/sig, u32 time, varint kind) + 362 B tags + 377 B content.
Two further steps the measurement points at, both expected to land the whole store at **~1.7–2 GB**:

- **Interned public keys.** Tags are the biggest part, mostly `p` tags in follow/mute lists repeating the
  same 32-byte keys. A key table (pubkey → u32) makes each reference ~3 bytes.
- **Ciphertext as bytes.** Much content is NIP-44/NIP-04 base64 (DMs, gift wraps, private `pcai:` docs);
  base64 does not compress and costs 33%. Store the decoded bytes; re-encode on the way out (lossless —
  the signature covers the original string, which is reproduced exactly; anything that does not round-trip
  byte-for-byte is stored as-is).

## Design

**RAM-first.** Every record and every index lives in memory. Disk is only durability: an append-only log
plus periodic snapshots. Reads never touch disk. A node with less RAM than the store (a PosterChanOS
desktop) evicts the CONTENT of old, unpinned events to the log and keeps their index entries — the same
code path, a smaller budget.

1. **Records.** One contiguous arena per ~64 MB slab; an event is `seq → (slab, offset)`. Fixed part, then
   tags (letter + interned-key / raw-32-byte / string), then content (stdlib `zlib` with a preset
   dictionary built from this relay's own posts — measured 398 B/event vs zstd's 386, so no new
   dependency — or raw bytes for ciphertext).
2. **Indexes — flat arrays, never a Python object per event.** In Python the danger is object
   overhead, not data: a dict entry or small object per event or per tag is 60–150 bytes, which across
   2.6M events and 16M tag references would cost more RAM than the data. So every index is numpy:
   - per-event columns indexed by sequence number: `created_at` u32, `kind` u16, author (interned) u32,
     id prefix u64, record offset u64 — **69 MB** for 2.64M events (measured);
   - postings in CSR form: a sorted u64 array of hashed keys (author+kind, kind, each single-letter tag
     value) with offsets into one u32 array of sequence numbers — **~154 MB** (measured);
   - new events go to a small append buffer (plain Python lists, bounded) that is merged into the arrays
     at every flush, so the big arrays are rebuilt in bulk, never grown per event.
   A NIP-01 filter: `searchsorted` each key, slice its postings, `intersect1d`/`union1d`, then the newest
   N by `created_at` (`argpartition`). Replaceable/addressable events keep a current-version map; a
   superseded version gets a dead bit in a bitmap array.
3. **Writes — delayed and configurable.** Writes land in RAM and are flushed to the log on a timer.
   - `posterchandb_flush_interval` — seconds between flushes, **default 60** (Admin → Nostr Relay).
     Everything written since the last flush is lost only if the machine dies WITHOUT a clean stop
     (power cut, kernel panic, OOM kill); copies of other relays' events are simply fetched again.
   - **A clean stop always flushes first.** SIGTERM (systemd stop/restart, `sync.sh`), SIGINT and SIGHUP
     write everything out before the process exits; the unit's `TimeoutStopSec` allows for it, and the
     relay refuses new writes while the final flush runs so nothing slips in after it.
   - `posterchandb_direct_durable` — **default ON**: an event PUBLISHED HERE (`origin='direct'`, 14% of
     rows — somebody's own post, DM, settings document) is flushed before the relay answers OK,
     group-committed so it costs 1–5 ms rather than a sync per event. This is the one write that is often
     the only copy, and the relay has told its author "saved". Off = it waits for the timer like the rest.
   - Admin shows "unflushed: N events, last flush Xs ago" so the window is never invisible.
4. **Durability.** Log records carry a CRC; a torn tail after a crash is detected and dropped, never read
   as data. Startup = latest snapshot + replay of the log tail. Backups = copy closed log files + snapshot.
5. **Deletes / expiration / auto-clean / pay-to-stay.** Dead bits; background compaction rewrites old
   slabs and log files without them. Today's SQL rules become bitmap queries over the same indexes.
6. **Search — at least as good as today.** Today NIP-50 search is Postgres's `simple` text config:
   lowercased words, no stemming, every query word must match (`plainto_tsquery`), newest first — over
   the content of EVERY event, ciphertext included. PosterChanDB keeps exactly those rules with an in-RAM
   word index in the same CSR form (word hash → sorted sequence numbers): **≤ 264 MB** measured (an upper
   bound — the vocabulary grows slower than the corpus) against Postgres's 2.38 GB. Ciphertext (NIP-04/44
   base64, 20% of events) is not indexed: it only ever produced noise words. A query = intersect each
   word's postings, newest first. The switch is gated on a parity test: a corpus of real search queries
   run against both stores, results compared id for id; any difference must be explained (e.g. Postgres's
   parser treating a URL as one token) before reads move.
7. **Shape — Python, inside the relay process.** numpy (already required on every node type, Nostr-only
   included) and the standard library (`zlib`, `mmap`, `os.pwrite`/`fsync`); no new dependency. Reads
   are numpy array operations measured in microseconds, run on the event loop; flushes, snapshots and
   compaction run in a worker thread (file I/O and numpy release the GIL). App tables (users, Blossom
   ownership; < 1 GB) stay in Postgres on servers.

**RAM, all in (measured, 2.64M events):** columns 69 MB + postings 154 MB + search ≤ 264 MB ≈ **0.5 GB**,
plus the content cache — its size is the `posterchandb_read_cache_mb` setting. Postgres today: 20 GB of
shared buffers.

## Read- and write-efficient: SSD writes (measured)

Postgres on server1 wrote **14.65 GB of WAL per day** (3.2 TB in the 220 days since its stats were reset),
before counting the data files written again at each checkpoint. About **49,000** events arrive per day,
so that is roughly **300 KB of SSD writes per stored event** whose compact form is 872 B — around 340×
write amplification. Most of it is **full-page images** (382 million of them, ~3 TB): after every
checkpoint the first change to each 8 KB page logs the whole page, and a new event dirties pages in seven
`events` indexes and three `event_tags` indexes scattered across the disk.

PosterChanDB writes each event ONCE, sequentially:

| | per day (49k events) |
|---|---|
| log append (872 B/event, batched per flush into large sequential writes) | **~43 MB** |
| index snapshot (~340 MB) — only on a clean stop and once a day (`posterchandb_snapshot_hours`, default 24) | ≤ 340 MB |
| compaction — a log file is rewritten only when **> 40%** of it is dead (`posterchandb_compact_dead_pct`) | proportional to deletes |

…about **0.4 GB/day against 14.65 GB/day**, roughly 35× fewer bytes written, and sequential rather than
random. Indexes are never written per event: they live in RAM and can always be rebuilt from the log (the
snapshot only makes startup fast). Reads touch no disk at all while the content fits the read cache.

Things that keep it that way:
- **Batching is the point of the write delay.** A 60 s flush turns thousands of small writes into a few
  large aligned ones; the per-write overhead of an SSD (erase-block rewrites) is paid once per batch.
- **Replaceable churn does not rewrite anything.** A settings document saved 100 times is 100 appended
  records with 99 dead bits — reclaimed in bulk by compaction, not in place.
- **No double-write.** The log IS the data; there is no separate WAL plus heap plus index pages.

Postgres, until then (each a config change on the database host, needs your OK): `wal_compression = lz4`
(shrinks full-page images 50–70%), a longer `checkpoint_timeout` with a larger `max_wal_size` (fewer
checkpoints → far fewer full-page images), and dropping the 2.4 GB search index nobody uses (one less
index dirtied per event).

## Settings (Admin → Nostr Relay → PosterChanDB)

All three are ordinary admin settings: declared in `SettingsResponse` (or they never hydrate — see
CLAUDE.md), an input on the Nostr Relay tab, stored like every other setting, and applied LIVE (no
restart) by the store when the value changes.

| setting | default | what it does |
|---|---|---|
| `posterchandb_read_cache_mb` | **auto** (the whole store if it fits in 50% of RAM, else 25% of RAM) | RAM for event CONTENT. Indexes are always fully in RAM (~340 MB here). Above the budget, the content of the oldest, least-read, unpinned events is evicted and read back from the log on demand. `0` = everything in RAM. |
| `posterchandb_flush_interval` | **60 s** | The write delay: seconds between flushes of buffered writes to disk. Anything since the last flush survives a clean stop (always flushed first) but not a power cut. |
| `posterchandb_direct_durable` | **on** | Flush this server's own users' writes before answering OK (group-committed, ~1–5 ms), whatever the interval. Off = they wait for the timer too. |
| `posterchandb_snapshot_hours` | **24** | How often the in-RAM indexes are snapshotted (plus always on a clean stop). Rarer = fewer SSD writes, a slightly longer startup after a crash (the log tail is replayed). |
| `posterchandb_compact_dead_pct` | **40** | Rewrite a log file only when this much of it is deleted/superseded — the knob between disk space and SSD writes. |

The tab shows the live numbers beside them — content in RAM vs on disk, cache hit rate, events waiting
to be flushed, seconds since the last flush — so the trade being made is visible, not inferred.
Pinned and never evicted: every replaceable/addressable document (profiles, follow lists, `pcai:` app
data) and anything published here, so the content that matters most is never a disk read.

## PosterChanOS

A PosterChanOS machine that runs the PosterChan server today needs a whole PostgreSQL server for its
relay (`app-misc/posterchan-server` depends on `dev-db/postgresql`). PosterChanDB needs no database daemon:
the relay keeps its events in its own files under the server's data directory. What that buys a desktop
or laptop:
- **RAM sized to the machine** — the read cache is a setting, and `auto` takes 25–50% of RAM; indexes for
  a personal relay (tens of thousands of events, not millions) are a few MB.
- **Laptop SSDs** — writes are batched (default every 60 s) and sequential; idle means zero writes.
- **Offline and on battery** — no Postgres to start, no checkpoint I/O while the lid is shut; a clean
  shutdown flushes, and the next boot loads one snapshot.
- **One less service** in the package and in the installer; Postgres stays only for the server's app
  tables, and on a personal machine those could move into the same store later.

## What has to move

Everything that reads the relay's event tables directly needs the store's API instead: the relay itself
(`nostr_relay/store.py`), the auto-clean and paid-retention rules, admin and community stats, the git
`pre-receive` hook, and the bots' community queries.

## Rollout (no flag day)

1. Prototype: load a snapshot of the real data, answer the relay's real filter mix, compare every answer
   to Postgres byte for byte; publish size/RAM/latency.
2. Shadow writes: every event goes to both stores; reads still from Postgres.
3. Shadow reads: every query runs on both and mismatches are logged (no content, counts and ids only).
4. Switch reads, one node at a time, starting with nas.lan. Postgres stays as the fallback.
5. Retire the Postgres event tables once a node has run clean for an agreed period.

Every data-safety rule the relay has learned keeps its test against the new store before it is trusted:
a read that could not answer is never "empty", a replaceable document is never replaced on the strength
of a failed read, nothing is purged on a partial read.

## Independent of all this

Make direct writes durable in Postgres now (the same rule as `posterchandb_direct_durable`) (`SET LOCAL synchronous_commit = on` for `origin='direct'`
inserts); keep it off for copies.
