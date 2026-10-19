# PosterChanDB — a RAM-first store for the relay's events

Status: **the relay's only store** (since 2026-10-10 on server1 and nas.lan; every fresh install; the PosterChanOS
desktop's local relay). The Postgres store was deleted in #195 -- there is nothing to choose and no other mode.
server1 on 2026-10-15: 4.08M live events (4.70M with not-yet-compacted dead ones), 4.6 GB on disk, 758 MB of index.
The short, benefits-first version is [POSTERCHANDB_OVERVIEW.md](POSTERCHANDB_OVERVIEW.md). Numbers here are MEASURED on server1's
live relay (a 2% sample, scaled, by the since-retired `posterchandb_measure.py`), not estimated from first principles.

## Why (as measured before the move, 2026-10)

The relay's Postgres database was 15 GB, and 95% of it is the event store:

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
   - `posterchandb_flush_interval` — seconds between flushes, **default 300** (5 minutes) (Admin → Nostr Relay).
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

## Bigger than RAM: the index lives in files (2026-10-16)

The design above held every index in RAM and a snapshot rewrote all of it. That stops scaling well before the
disk does: measured at 4.72M events, the index was ~200 bytes an event of RAM on top of the read cache (the
`#d~` prefix index alone was 1.36M Python tuples) and the snapshot was 1.24 GB. At 200 GB (~260M events) that
is ~50 GB of RAM before a byte of cache, and ~50 GB written per snapshot -- impossible on a small VPS.

Now (`disk.py`, `checkpoint.py`):

- **Columns are files** (`col-<name>.bin`), mapped copy-on-write. Reading one is reading the page cache; the
  kernel keeps the pages queries touch and drops the rest. Writes stay private until a checkpoint.
- **Index runs are files** (`run-<kind>-<n>.<i>.bin`, mapped read-only): postings (`idx`, `w` = search words),
  the id lookup (`id`), the `#d~` prefix index (`dp`, a byte blob + offsets) and the author lookup (`au`). New
  entries collect in a small RAM run; past a size, and at every checkpoint, it is written out. Disk runs merge
  in the background (maintenance pass, paced like compaction), STREAMED a key range at a time, so a merge never
  holds more than a chunk in RAM whatever the size of the runs.
- **A checkpoint writes what changed** (`MANIFEST.json`): log fsynced → RAM runs and new column rows written where
  the previous checkpoint does not look → changed pages (kills, compaction moves) journaled (`journal.bin`) →
  manifest replaced atomically → journal applied. A crash at any step opens on the old manifest or re-applies
  the journal (`tests/test_posterchandb_checkpoint_crash.py`). It is cheap, so **every clean stop takes one**:
  a restart replays nothing (measured on a copy of poster.place, 4.88M events: start **1.3 s**, was 20.6 s).
- **A compaction checkpoints before it deletes the segment**, so the next start no longer replays the whole log
  after every compaction.
- **One RAM setting.** `posterchandb_read_cache_mb` covers the event cache AND the index pages in RAM; over it
  (or under memory pressure) the governor hands index pages back (`Store.trim_index`). Set it low on a small VPS.
- **Measured** (same copy): RAM after start (not counting the event cache) **195 MB, was 1,797 MB**; 284 sampled
  queries answered identically. `tests/test_posterchandb_scales_past_ram.py` holds anonymous RAM flat as the store
  grows (186 → <16 B/event), a checkpoint after 2,000 events to < 4 MB (was 27 MB at 200k events), and opens and
  queries a store **inside an 88 MB memory cgroup** that is bigger than the cap (the old store is OOM-killed there).
- **A lookup reads one page of keys** (fence pointers, `disk.search`, 2026-10-18). The first key of every 4 KB page of
  a run's keys is kept in RAM (0.2% of the run), written beside it as `run-<name>.<k>.bin` -- the checkpoint's sweep
  removes it with its run, a torn or foreign fence file is ignored, and a run written before fences existed computes
  them once on first use. A binary search over the mapped keys was ~20 random page faults; it is now a search in RAM
  plus one page. Single-key lookups and batches of up to 64 keys go through the fences; a bigger batch (a feed of
  thousands of authors) keeps the one vectorised search. `tests/test_posterchandb_run_fences.py`.
- **Index runs are mapped `MADV_RANDOM`.** Without it a page fault reads around the device's whole readahead window
  (2 MB on an md RAID, 8 MB on its disks, 128 KB on NVMe). `tests/test_posterchandb_runs_are_random_access.py` reads
  the kernel's own `VmFlags`. Columns stay on default readahead: they are scanned sequentially.
- **Housekeeping's merges survive a quiet stop.** A clean stop checkpoints when log records OR merged/sealed runs are
  unsaved (`Store._unsaved()`); it used to look at log records only, so an idle store (a PosterChanOS desktop's local
  relay above all) threw its merges away at every stop and redid them at every start.
  `tests/test_posterchandb_merges_survive_a_quiet_stop.py`.
- **`since`/`until` cut a posting list before the union.** A poll naming many tags or authors "since a moment ago" (the
  push service's `#p` of every subscriber; a negentropy sync of "my follows' last week") unioned and sorted each
  key's whole history first -- `np.unique` was ~10% of the relay's CPU. A list of 256+ postings is filtered on the
  `created` column first. `tests/test_posterchandb_since_before_union.py`.
- **Migration** is automatic: the first start with this code imports the old `index.snap` into the new files
  (~65 s on poster.place's copy, once), checkpoints, and deletes `index.snap`.

### Measured at 30M events (2026-10-18, `scripts/pcdb_scale_check.py`)

A synthetic 30M-event store (10.5 GB on disk; `ScaleGen` kinds/tags/words) built on spinning disks (an md RAID of
5,400 rpm WD drives -- the worst case), its runs merged the way housekeeping would (`compact`), opened COLD inside a
memory cgroup at the owner's target ratio of 200 GB : 30 GB (1.6 GB for 10.5 GB) with no swap:

| query (p50 / p99) | runs unmerged | merged | merged + fences |
|---|---|---|---|
| tag (`#t`, 50) | 639 ms / 4.8 s | 69 ms / 1.6 s | **22 ms** / 1.5 s |
| 5 ids | 1,370 ms / 2.1 s | 402 ms / 1.2 s | **236 ms** / 0.57 s |
| author (50) | 2,449 ms / 6.1 s | 843 ms / 4.4 s | **629 ms** / 4.3 s |
| search | 838 ms / 1.3 s | 43 ms / 0.23 s | **40 ms** / 0.18 s |
| timeline (120) | 7.8 ms / 1.3 s | 4.6 ms / 0.48 s | **4.6 ms** / 0.12 s |

Opened in 0.0 s (checkpoint, nothing replayed); anonymous RAM ~90 MB, the rest of the cap is page cache. What remains
is physics on a spinning disk: an author's 50 posts are 50 records scattered across a 10 GB log, ~10 ms each cold. On
the NVMe production runs on, a cold record read is ~0.1 ms. The build itself (`build`) flushes every million events;
unflushed, a build is a RAM test, not a store test.

## Read- and write-efficient: SSD writes (measured)

Postgres on server1 wrote **14.65 GB of WAL per day** (3.2 TB in the 220 days since its stats were reset),
before counting the data files written again at each checkpoint. About **49,000** events arrive per day,
so that is roughly **300 KB of SSD writes per stored event** whose compact form is 872 B — around 340×
write amplification. Most of it is **full-page images** (382 million of them, ~3 TB): after every
checkpoint the first change to each 8 KB page logs the whole page, and a new event dirties pages in seven
`events` indexes and three `event_tags` indexes scattered across the disk.

PosterChanDB writes each event ONCE, sequentially. The design budget (49k events a day):

| | per day |
|---|---|
| log append (872 B/event, batched per flush into large sequential writes) | **~43 MB** |
| checkpoint — at a clean stop, after a compaction, and at most every `posterchandb_snapshot_hours` | only what changed since the last one |
| compaction — a log file is rewritten only when **> 40%** of it is dead (`posterchandb_compact_dead_pct`) | proportional to deletes |
| index-run merges (2026-10-16) — new index entries stay in RAM until a checkpoint or until a run passes a few tens of MB, then are written once; disk runs are merged in the background, so an entry is rewritten about log₄(store / run) times in its life, paced by the maintenance cap | estimated ≤ 150–200 MB on poster.place (not yet measured) |

**Measured on server1, 2026-10-15 — the snapshot term dominates, and it is not small.** The index snapshot is
now **1.24 GB** (4.7M events, 758 MB of index; ~260 B an event), and with the default then of **1 hour**
(the store's housekeeper since primary mode, 2026-10-10, and the admin tunable copied it) a busy relay always has
something new, so it wrote one every hour. The relay process wrote **18.0 GB in 10.3 h**
(`/proc/<pid>/io` write_bytes), about **42 GB/day**. Ten hourly snapshots account for ~12 GB of the 18; the rest
(log appends, one compaction of 11 segments, sidecar files) has not been broken down. It is sequential, but it is MORE than Postgres's 14.65 GB/day of WAL (whose checkpoint
data-file writes were never measured), and nowhere near the 0.4 GB/day this section used to promise. A clean stop always
snapshots, so the periodic one only bounds how much log a CRASH replays at the next start; so the default is now
**24 hours** (2026-10-15): about 28 GB/day less, for ~13 s more replay after a crash (measured: ~20k events/s,
~260k events/day). `posterchandb_snapshot_hours` (Admin → Nostr Relay → Database, live) still sets it.

Indexes are never written per event: they live in RAM and can always be rebuilt from the log (the snapshot
only makes startup fast). Reads touch no disk at all while the content fits the read cache.

Things that keep it that way:
- **Batching is the point of the write delay.** A 5-minute flush turns thousands of small writes into a few
  large aligned ones; the per-write overhead of an SSD (erase-block rewrites) is paid once per batch.
- **Replaceable churn does not rewrite anything.** A settings document saved 100 times is 100 appended
  records with 99 dead bits — reclaimed in bulk by compaction, not in place.
- **No double-write.** The log IS the data; there is no separate WAL plus heap plus index pages.
- **Progress files are saved hourly, never per pass.** The relay's bridge/mirror scans keep their progress in
  `scan-memo.json`; it was rewritten WHOLE (43.5 MB on server1, every historic hit) on every incremental pass,
  i.e. on every reload-blocks (2026-10-18). It is now saved at most once an hour and at a clean stop, and hits whose
  events have died are dropped on save so it stops growing. A stale memo only means a longer rescan after a crash.
  `tests/test_pcdb_scan_memo_writes.py`.


## How the database cleans itself (and when)

Two layers: the RELAY decides what to delete, the DATABASE gives the space back. Nothing here needs an operator.

**What gets deleted -- the relay's rules (`nostr_relay/thread.py`, the same rules on every store):**

| When | What |
|---|---|
| **Auto-clean, once a day** -- checked 5 min after the relay starts and then every 24 h, with a persisted stamp so a restart neither skips nor repeats it (`nostr_relay_prune_interval_sec`, default 86400) | other people's synced feed posts older than `nostr_relay_retention_days` (default **30**), expired events (NIP-40), retired kinds, the count cap if `nostr_relay_max_events` is set. NEVER a direct write (members' posts, settings, Notes, calendars) and never a preserved author. Admin → Nostr Relay → **Run auto-clean now** runs it at once. |
| **Block purge, nightly between 02:00 and 04:00** | events from blocked accounts, words, languages and bridges |

Measured on server1: the auto-clean of 2026-10-15 14:53 removed 57,004 events; the block purge of 2026-10-16 02:47
removed 282.

**How the space comes back -- this database (`maintenance.py`, `store.compact`, `disk.py`):**

1. **A delete is a dead bit.** The event disappears from every answer at once; its bytes stay in its segment file
   (and an `OP_DEAD` marker is appended to the log, so the decision survives a restart).
2. **Compaction**, every housekeeping pass (**10 min**, `posterchandb_maint_interval_sec`): one closed ~256 MB
   segment that is more than **40 %** dead (`posterchandb_compact_dead_pct`) has its live records copied forward
   and its file deleted -- after a checkpoint that no longer names it.
3. **The index cleans itself**: a dead event's postings, id entry and `#d~` entry are dropped whenever the run
   holding them is merged -- the RAM runs at each flush, the disk runs in the same housekeeping pass (up to 4 merges a
   pass, streamed).
4. **The disk guard**: below **5 %** free on the filesystem, the OLDEST re-fetchable copies (synced feed posts)
   go first until it recovers. Never a direct write, never a preserved author.

**What housekeeping may cost** (all live in Admin → Nostr Relay → Database): at most **8 MB/s** of writes
(`posterchandb_maint_io_mb_s`), at most **10 %** of a CPU (`posterchandb_maint_cpu_pct`), idle CPU and IO priority,
and it waits while the machine is busy (`posterchandb_maint_busy_load`). So a cleanup of a million events is a few
MB of appended markers at the time, and its disk is given back a segment at a time over the following passes.

## Settings (Admin → Nostr Relay → Database)

Declared in `SettingsResponse` (or they never hydrate -- see CLAUDE.md), defined with their ranges in
`app/services/relay_tunables.py` (`PCDB`), inputs in `templates/admin/tabs/nostr_relay.html` (the Database section).
**Every one applies to the RUNNING store** (`relay_tunables.LIVE_KEYS` → `posterchandb/tuning.apply`): each is an
attribute its loop re-reads on every step, so Save changes it without a relay restart and without reopening a file.
An out-of-range value is clamped to the range below.

| setting | default (range) | what it does |
|---|---|---|
| `posterchandb_flush_seconds` | **300** s (1–3600) | The write delay for SYNCED copies: new events reach disk in one sequential batch this often. A crash loses at most this window of copies, which are fetched again. Direct writes are not in it -- they are fsynced before the relay answers OK. |
| `posterchandb_read_cache_mb` | **0 = auto** (30% of RAM) | RAM for older log segments AND the index pages cached from disk -- the one number to set on a small machine. |
| `posterchandb_cache_low_water_mb` | **0 = auto** | The free-RAM floor at which the cache governor drains the read cache (MemAvailable + PSI, `cache.py`). |
| `posterchandb_snapshot_hours` | **24** h (0.25–48) | An index snapshot at most this often (and on a clean stop), so a restart loads the snapshot and replays only the log after it. |
| `posterchandb_compact_dead_pct` | **40** % (10–90) | A closed segment is rewritten once this share of it is dead (deleted, expired, replaced). |
| `posterchandb_maint_interval_sec` | **600** s (60–86400) | How often the housekeeping pass (compaction, index-run merges, checkpoint when due) runs. The relay's auto-clean has its own daily schedule (see "How the database cleans itself"). |
| `posterchandb_maint_io_mb_s` | **8** MB/s | Housekeeping's write ceiling, so it never competes with the live relay or wears an SSD in bursts. |
| `posterchandb_maint_cpu_pct` | **10** % | Housekeeping's CPU share. |
| `posterchandb_maint_busy_load` | **0.75** load/CPU (0 = never wait) | Housekeeping waits while the machine is busier than this. |
| `posterchandb_words_batch` | **25** events | Search words indexed per background step (the store lock is held ~25 ms per step; 300 held it ~90 ms). |

The Database section's status line reads `posterchandb` from the relay's status file
(`data/nostr_relay.status.json`): state, live/dead event counts, bytes on disk, index bytes, segments.
The data directory is `$POSTERCHANDB_DIR` (set it in `data/secrets.env`; Docker sets it), else
`/var/lib/posterchandb`; `scripts/install/posterchandb.sh` creates it. The relay's store is `<dir>/relay`.

## PosterChanOS

Two different uses, both PosterChanDB:

- **A PosterChanOS machine running the PosterChan server** needs no PostgreSQL server (`app-misc/posterchan-server`
  dropped its `dev-db/postgresql` dependency in -r6, and `pc-server enable` no longer initialises a cluster). Its
  relay is the server relay above, with the same settings.
- **Every PosterChanOS desktop runs a LOCAL relay as the desktop app's event cache** (`relay_main.py --os-mode`,
  `app/services/nostr_relay/osmode.py`, package `app-misc/posterchan-relay`, user unit `posterchan-relay.service`).
  It replaced the app's IndexedDB, which measured 10-31x write amplification (327 MB / 1.2 GB written in 19 minutes
  on the desktop / laptop). One store per ACCOUNT under `~/.local/share/posterchan/relay` (`PC_RELAY_DIR`), loopback
  only (it refuses any other bind), 32 MB segments, flushed every **30 s** (`PC_RELAY_FLUSH_SECONDS`), and an LRU
  byte cap of **512 MB** (`PC_RELAY_MAX_MB`) that never evicts the client's pinned documents (`os_pins.py`, a copy
  of `store.js` `_isPinned`, kept equal by `test_os_relay_pins_match_the_client.py`). Its only upstream is the
  instance chosen on the welcome screen; none = a pure cache. The client never depends on it: anything it refuses
  or does not acknowledge goes to IndexedDB. See CLAUDE.md "PosterChanOS's LOCAL RELAY".

What PosterChanDB buys a desktop or laptop: RAM sized to the machine; batched sequential writes (idle = zero
writes); no daemon to start and no checkpoint I/O on battery; a clean shutdown flushes and the next boot loads one
snapshot.

## What reads the store

Everything that used to read the relay's Postgres tables now goes through the store's API: the relay itself
(`nostr_relay/store.py` rules + `pcdb_store.py`), the auto-clean and paid-retention rules, admin and community
stats (`nostr_relay/aggregates.py`, computed inside the relay process), the git `pre-receive` hook (over the
relay's loopback socket), and the bots' community queries (`/api/community/*`). Nothing opens the store's files
from another process: the relay owns them.

## How it moved in (done; the migration code is deleted)

PosterChanDB replaced the relay's Postgres store without a flag day: a MIRROR beside Postgres copied a
repeatable-read snapshot and every later write (`off` → `shadow`, answers compared → `serve`, answered from RAM
with Postgres as the fallback), and a promotion script made a directory the relay's ONLY store once its live
count and id set equalled Postgres's. Both nodes have run on PosterChanDB alone since 2026-10-10, and the
desktops' local relays never had a Postgres. The Postgres store, the mirror, the promotion and parity scripts
and the `off`/`shadow`/`serve` modes were deleted afterwards (#195); `git log` has them.

## The store (`nostr_relay/pcdb_store.py`)

`PcdbRelayStore` is the relay's store; `nostr_relay/store.py` holds the rules every store obeys and the async
base it inherits. `thread._open_store` opens it under `<POSTERCHANDB_DIR>/relay`: an EMPTY directory becomes a
new store on the first start (`init_new` writes the `PRIMARY` marker); a directory holding events with no marker
is refused, never adopted -- nothing records whose data that is.

- **Events and tags**: the posterchandb `Store`, which applies the relay's ingest rules.
- **relay_kv / wot / bridge_nip05 / bridge_puppet**: a SIDECAR beside the segments, one file per table
  (`sidecar-<table>.json`), each replaced atomically on every change (temp file, fsync, rename, fsync of the
  directory). A SIGKILL at any instant leaves the last complete state; a file this code did not write is refused,
  never read as empty (an empty WoT/kv would forget the pinned, never-pruned authors).
- **Prune, preview, content purges**: store.py's rules, read from its constants, evaluated over the numpy columns
  in chunks -- 65,536 rows copied per lock hold, 512 deaths, 128 decoded records, 256 id lookups. Measured on 200k
  events: the longest store-lock hold of a whole prune / preview / word purge / author purge / COUNT / negentropy
  is **~3 ms** (`tests/test_pcdb_relay_store.py` prints it). The content scans run on their own thread; only the
  deletions go through the writer thread. The tiered (pay-to-stay) rules are DATA (`_tiered_specs`); the masks are
  built from them and `_tiered_rules` describes them, so the description a test reads is the rule that runs.
- **Durability**: a direct write is fsynced (`Store.sync`, fsync outside the lock) before the relay answers OK;
  purges and prune passes are synced when they finish; synced copies wait for the flush timer.
- **Search** while words replayed at startup are still being indexed is REFUSED (`SearchNotReady` → CLOSED with the
  reason), never answered empty.
- **Counts** (Server Stats, the NostrStats bot, the relay trace) are computed inside the relay process from copies
  of the columns (`nostr_relay/aggregates.py`). Until #195 they still looked for a mirror and fell through to a
  Postgres connection the store does not have -- every stat answered "unavailable" on both nodes.

**Two production lessons** (2026-10-09): the Postgres side deleted older versions WHILE comparing, so a
refused stale version could destroy versions depending on heap order — it now decides first, then deletes
(PosterChanDB always did). And a long transaction on the relay DB plus any DDL froze every read (a waiting
ACCESS EXCLUSIVE queues all later readers): the relay's startup housekeeping now uses `lock_timeout` and skips.

**Serve starved the relay the first night** (2026-10-09, clients "not reconnecting"), and every single-threaded
test passed: (1) a query with no selective set (the global feed, `kinds` + `limit`) intersected the whole
kind posting list and sorted it — 465 ms on server1's data, under the store lock, so the relay's few query
threads queued behind each other; it is now a newest-first backward scan that stops once the k-th newest is
provably newer than anything earlier (a running max of `created_at`, since rows are in arrival order): 4 ms.
(2) The background word indexer held the lock ~90 ms per 300-event batch; it takes 25. (3) A query waited up
to 0.5 s for queued writes, and under a firehose the mirror is always a little behind, so most queries waited;
the wait is 0.05 s and Postgres answers otherwise. `test_posterchandb_global_feed_at_scale.py` (300k events,
exact against brute force, 4 concurrent clients) fails on the old code. (The Postgres mirror and its load test were
retired with Postgres, #195.)

Every data-safety rule the relay has learned keeps its test against the new store before it is trusted:
a read that could not answer is never "empty", a replaceable document is never replaced on the strength
of a failed read, nothing is purged on a partial read.
