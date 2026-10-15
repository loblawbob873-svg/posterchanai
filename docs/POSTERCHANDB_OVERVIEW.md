# PosterChanDB — what it is and why PosterChan uses it

PosterChanDB is the event store behind PosterChan's Nostr relay. It replaced PostgreSQL as the place the relay
keeps its events, and it is what a fresh PosterChan install uses: **no install path sets up Postgres any more**.
This page is the short version — what it does and what it buys you. The design, the measurements behind every
number here and the operational details are in [POSTERCHANDB.md](POSTERCHANDB.md).

## In one paragraph

Every event and every index lives in RAM, as compact flat arrays rather than one object per event. Disk is only
for durability: an append-only log written in large sequential batches, plus an occasional snapshot of the
indexes so a restart is fast. A Nostr filter (`authors`, `kinds`, `#e`, `#p`, `since`/`until`, `limit`) is
answered by intersecting sorted arrays in memory, so a read never touches the disk. It is pure Python plus
numpy, ships inside the app, and runs in the relay's own process — there is no database server.

## Status (2026-10-15)

PosterChanDB is the relay's ONLY store -- the Postgres store was deleted (#195), so there is no mode to pick.

| where | since | on disk |
|---|---|---|
| server1 (poster.place) | 2026-10-10 | 4.6 GB (4.08M live events, 758 MB of index) |
| nas.lan | 2026-10-10 | — |
| fresh installs (`install.sh`, Docker, a PosterChanOS server) | the first event | grows with use |
| every PosterChanOS desktop: the LOCAL relay that caches the desktop app's events, one store per account (512 MB LRU cap) | 2026-10-12 | ≤ 512 MB |

## The benefits

### 1. A fraction of the space

Measured on server1's 2.64 million events:

| | size |
|---|---|
| Postgres (events + tags + their indexes) | ~15 GB |
| the same events as plain JSON | 3.9 GB |
| **PosterChanDB records** | **2.3 GB** |
| PosterChanDB indexes (in RAM) | ~340 MB |

Postgres stored each event's 64-character hex id again in every one of its 16 million tag rows, and kept a
2.4 GB full-text index that was used 1,779 times in its life. PosterChanDB stores ids and keys as raw bytes,
packs tags, and compresses content with a dictionary trained on the relay's own posts.

### 2. Sequential SSD writes, written once

| | written per day |
|---|---|
| Postgres (WAL alone, before checkpoints; ~49,000 events/day) | **14.65 GB** |
| PosterChanDB as designed (log + one daily snapshot) | ~0.4 GB |
| **PosterChanDB on server1, measured 2026-10-15** (4.7M events, hourly 1.24 GB snapshots) | **~42 GB** |

The design figure assumed one snapshot a day; the store ran hourly from 2026-10-10 and the snapshot grew with
the store. Back to a day since 2026-10-15. The interval is a live setting (`posterchandb_snapshot_hours`); see POSTERCHANDB.md.

Postgres wrote roughly 300 KB per stored event: every new event dirtied pages in ten indexes scattered across
the disk, and after each checkpoint the first change to every page logged the whole 8 KB page. PosterChanDB
writes each event once, appended to a log in large batches. Indexes are never written per event — they live in
RAM and can always be rebuilt from the log. Less writing means a longer-lived SSD, which matters most on the
small machines PosterChanOS runs on.

On btrfs the store's directory is set NOCOW (`chattr -R +C`) by every install path — `install.sh`, the Docker
entrypoint, the PosterChanOS installer and `pc-server` on every enable — so the filesystem does not
copy-on-write and fragment an append-only log.

### 3. Reads never wait on the disk

Every index and, within the read cache, every record is in memory. A query is a handful of `searchsorted` and
array intersections. The relay's heaviest jobs — a prune, a purge of a blocked author, a COUNT, a negentropy
sync — hold the store's lock for about **3 ms** at a time (measured on 200k events), so clients are never
starved while maintenance runs.

### 4. Your own posts are safer

Postgres ran with `synchronous_commit = off` for the whole database, so a power cut could lose a post after the
relay had already told the client "OK" — and a person's own post is often the only copy. PosterChanDB fsyncs a
direct write **before** the relay answers OK. Copies synced from other relays are batched (they exist
elsewhere); your writes are not.

### 5. One less thing to run, and one less way to freeze

There is no database server to install, tune, upgrade, vacuum or back up separately — the relay's data is one
directory. It also removes a class of outage we actually had: on Postgres, a long transaction plus any schema
change queued every later reader behind a lock, and the relay looked "empty" to everyone until it cleared.
PosterChanDB has no DDL and no lock queue of that kind.

### 6. It moves in without an outage

The rollout never had a flag day. A node went `off` → `shadow` (every write mirrored, a sample of answers
compared against Postgres) → `serve` (answered from RAM, Postgres still written as a fallback) → `primary`, and
promotion refuses unless the live count and the id set match Postgres exactly. Copying server1's 2.6 million
events took 4.5 minutes. The rest of the app's own data moved off Postgres the same careful way (relay documents
per table, see CLAUDE.md "#161").

### 7. Built for growth

Indexes are flat numpy arrays — a Python object per event would cost more RAM than the data itself at this
scale. Nothing does O(all events) work per request, per page or per restart, and the store is designed for far
more than today's 3+ million events.

## What it costs, honestly

- **RAM.** The indexes (~340 MB for 2.64M events) and the read cache live in memory. A machine with less RAM
  than the store evicts old, unpinned content to the log and keeps the index entries — slower for that content,
  never wrong.
- **A crash window for synced copies.** Events synced from other relays are flushed every 5 minutes
  (`posterchandb_flush_seconds`). A crash can lose up to that window of *copies*; they are fetched again.
  Direct writes are not in that window — they are fsynced first.
- **Snapshot writes.** The index snapshot (1.24 GB on server1) is rewritten every `posterchandb_snapshot_hours`
  while anything new arrived. At the old 1-hour default that measured ~30 GB/day on server1; the default is 24 h now. A clean stop snapshots anyway;
  the periodic one only shortens a crash restart. See POSTERCHANDB.md, "SSD writes".
- **Search while starting.** Full-text search is refused (with a reason) until the words replayed at startup
  are indexed, rather than answering empty.

## Settings

Admin → Nostr Relay → Database: the write delay, the read cache and its low-water mark, the snapshot interval,
the compaction threshold, and the housekeeping cadence and its I/O, CPU and load limits. Every one applies to
the running relay — no restart. There is no store to choose. Details: [POSTERCHANDB.md](POSTERCHANDB.md).
