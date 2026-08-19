# Folder Sync

Documents and Pictures kept in step across your devices, in encrypted Blossom, the way Notes and
Music already are. Sidebar → **Folder Sync**.

**Desktop and Android only.** A browser cannot sync: it has no filesystem, and Firefox has no File
System Access API at all. Every device can still *read* a synced folder — see
[Browsing without syncing](#browsing-a-folder-on-a-device-that-does-not-sync-it).

---

## The pair key is the whole feature

Two devices sync together because **you gave the folder the same name on both**. That name — the
*pair key* — is what the shared record of the folder is stored under; where the folder actually lives
is per device, and stays on that device.

```
laptop   ~/Documents          ─┐
phone    /storage/…/Documents ─┴─► one manifest, "Documents"
desktop  D:\Work\Docs         ─┘   (same name typed on each)
```

So "Documents" on the laptop meeting "Docs" on the phone is **two folders, not one**, and nothing
later will explain why they never meet. The screen asks for the name and says so.

This was not always true, and it is worth knowing why it is stated this loudly: the manifest used to
be keyed on the *platform's* handle for the directory — a random id on desktop, a SAF tree URI on
Android — which is device-local by construction. Every device wrote and read a different document, so
each one synced happily with itself and cross-device sync could not work at all.
`tests/client/exec_sim.js` now runs two and three independent devices against one folder so that
cannot come back silently.

## One versioned record per file

The folder's shared state is **one record per file** — `pcai:fs:<pair>:<sha256(path)>` on this
node's local relay — and the server refuses any write that is not **strictly newer** (a higher `v`)
than the record it replaces, decided under a lock. That is the whole design, and it is what makes
the failures of the two earlier engines impossible rather than guarded:

- **No read can empty the folder.** There is no document whose absence, emptiness or staleness
  describes more than one file. A record that fails to load is one file the sweep leaves alone —
  in the safe direction, which is *doing nothing*.
- **A deletion is a record, never an absence.** Deleting publishes a tombstone at a higher
  version, carrying the file's last address so any device can restore it account-wide. A path with
  no record is a path nobody has said anything about.
- **Two devices cannot silently overwrite each other.** Every write goes through the server's
  per-file compare-and-swap: the loser of a race is *refused*, learns immediately, and resolves the
  divergence as a conflict on its next sweep — both copies survive.
- **A device's past life cannot haunt it.** Removing a folder for the account bumps its **era**
  (one integer): every existing record is instantly of a dead world, and a device that comes back
  with a journal from that world clears it and rejoins by content. Re-adding a folder cannot mint
  ghost conflicts.
- **The server backstops mass deletion.** A batch that tombstones more than 100 files is refused
  outright unless it came through the deliberate-delete confirmation — a second, server-side copy
  of the client's own guard, for the client that has gone wrong. It is a *backstop*, not the guard:
  the client's own floor is 20, and everything between 20 and 100 is exactly the band a wave of
  stale tombstones used to cross in silence.
- **A tombstone keeps the address of what it deleted, taken from the shared record OR the journal —
  whichever still has one.** Both executors read `index[p] || state[p]`, which reads as "prefer what
  we applied" and is how the address goes missing: a journal entry that had lost its own (a struck
  CAS write, an era change, a row from an older build) shadowed a record that still had one. An
  address-less tombstone breaks two things at once and reports neither — *Deleted on every device*
  cannot offer the file, because it can only restore what it can address; and no device still holding
  the file can ever settle against the deletion, because *delete loses to edit* compares checksums and
  an absent checksum always reads as an edit. That device republishes for ever and trips the
  resurrect floor for ever, which is the standoff reported as "it always wants to republish".

Each device also keeps a **journal** (what it applied, and what each file looked like on disk when
it did) — the third input the reconcile needs to tell "changed here" from "changed there". Reads
are **cache plus delta**: the record set lives in IndexedDB and each sweep asks only for records
written since its last look, so a 12,000-file folder costs one full read ever.

## What is encrypted, and what this node can see

File **contents** are AES-256-GCM under your drive's master key before they are uploaded, so a blob
is ciphertext to everyone including this node. The **manifest** — every path and size — is NIP-44
encrypted to your own key on top of that, so the node stores a document it cannot read either.

What the node does see, unavoidably: how many live entries a folder has (one plaintext number), how
many blobs there are and how big each one is, and when they arrived. And because identical bytes
deduplicate, somebody holding a candidate file can confirm whether that exact file is stored. That is
the trade every deduplicating encrypted store makes.

This is a **stronger** guarantee than Calendar and Contacts, which are encrypted with a key this node
holds because a CalDAV client sends plaintext and the server has to answer it. Nothing here ever needs
the server to read a filename, so it never can.

## Safety rules

These are the rules the engine will not bend, each one a way to lose a file:

- **Nothing is deleted in place.** Every local deletion is a move into `.pc-trash/<date>/` inside the
  folder, so a bad manifest or a clock skew is recoverable with a file manager instead of a backup.
- **Conflicts keep both.** Two devices editing one document is a normal Tuesday, and arbitrary bytes
  have no correct merge. The incoming copy takes the real name and yours becomes
  `report (conflict from laptop, 2026-08-09).pdf`.
- **A delete loses to an edit**, in both directions. Resurrecting a file you meant to delete costs one
  more delete; the other way costs the file.
- **Excluding a folder never deletes it.** An exclusion means "stop looking at this", so an excluded
  path is dropped from every snapshot — including the shared one, which is what leaves another device
  that does *not* exclude it syncing happily.
- **Agreement advances per file, and only after that file has actually moved.** A sweep interrupted
  halfway resumes rather than restarting, and a failed upload never becomes a silent delete.
- **Stopping a sync deletes nothing** — on this device or any other. It stops being kept in step, the
  local agreement is dropped, and the folder's NAME is remembered, so picking it up again rejoins the
  same pair instead of asking you to name it a second time (a different answer there would be a
  second folder that never meets the first).
- **An empty agreement is not a conflict.** A device whose `base` was cleared sees both sides
  "changed" for every file at once; an ordinary sweep does not hash, so a convergence test that
  demanded checksums could never fire and the whole folder duplicated itself as conflict copies.
  Size and modification time settle it, the same comparison used for change detection everywhere.
- **A sweep may not republish more than 20 files your other devices deleted.** The mirror of the rule
  below, and the one that catches a restored machine. *Delete loses to edit* republishes a file the
  manifest says was deleted whenever it looks changed here — and on an ordinary sweep "changed" is
  size and modification time, because rehashing a 40 GB folder every time is the space heater the
  whole battery policy exists to avoid. So a device whose timestamps moved under it (restored from a
  backup, copied in, `rsync` without `-t`) reads *every* deletion as a local edit and refills the
  folder on every other device, including the ones that correctly applied the delete minutes earlier.
  Past a floor of 20 it asks; a background sweep, having nobody to ask, declines and leaves the
  deletions standing. Unlike the delete guard this is an absolute floor rather than a ratio, because
  a restore makes everything look edited — the resurrections arrive beside thousands of ordinary
  uploads that any ratio counts as "kept", and 3,930 beside 11,884 sails past every one of them.
  Refusing suppresses only the republishing, records nothing in the agreement, and asks again next
  sweep. **And the refusal now has a way out that is not the same dialog again**: the folder's card
  offers *"Put N files back everywhere"*, which sends those exact paths by name. A named path is not
  an inference from a timestamp — it is a person answering the question the floor exists to ask — so
  the floor does not apply to it, and one press converges the folder. It is reported by name, not as a tally: deciding whether to delete them again is a decision
  you can only make if you can see which files they are.
- **A sweep may not delete more than it keeps, and past 20 it may not delete in bulk at all.** Every
  rule above decides one path, and each of them is right — but a manifest that has gone wrong does
  not produce one bad decision, it produces ten thousand identical ones. Measured on a real Pictures
  folder: the shared manifest held ~10k paths and every single one was a tombstone (`n=0` live on the
  server), so re-adding the folder on the device that still had the files read "deleted elsewhere"
  for all of them and moved the lot into `.pc-trash`, correctly, per path, without a word. A sweep
  that would trash more than survives it stops and asks; a background sweep — the watcher, a resume,
  the heartbeat — has nobody to ask and so refuses.

  **The absolute floor is the same 20 as the rule above it, and for a long time it was not.** The
  proportional rule was paired with a cap of 100, and between the two there is a wide silent band:
  measured on the folder this was reported from, 59 stale tombstones over a 1,000-file pair passed
  the ratio (59 is nothing beside 1,000 kept) and passed the cap, so a laptop and then a tablet moved
  the same 59 files into `.pc-trash` with no verdict, no dialog and no line on the card — while the
  desktop, which still held every one of them, was refused by the resurrect floor at 20 and could
  only report "NOT republished — your other devices deleted these", on every sweep, for ever. **The
  guard written to protect the files is what guaranteed the deletions won**: the one device in a
  position to act was the only one forbidden to. A ratio cannot see a bulk deletion for the same
  reason it cannot see a restored backup — it arrives beside thousands of unchanged files. So the
  floor is absolute and it is one number in both directions, and whichever way the person answers,
  the folder converges instead of oscillating.

  The cost is real and is the right one: an explicit deletion of 20+ files made in Files is confirmed
  once where it was made, and then confirmed once more on each device as it applies. That is the
  price of never again applying a deletion nobody watched, and the dialog names the count and says
  which device is holding the copies. A refusal is **not recorded in the agreement**, so the next sweep
  re-proposes it and asks again, and only deletions are suppressed: uploads and downloads still run,
  because a guard that aborts the whole sweep turns "it deleted everything" into "it syncs nothing,
  for ever". The server's collapse guard cannot cover this — a mass *local* delete writes no
  manifest at all, it only advances `base`.

## Size

The old per-document ceiling (NIP-44 refuses a plaintext over 65535 bytes — about 376 files per
document) is gone by construction: a record is one file's metadata. The one place the seal can
still bind — a single file whose chunk list outgrows it (an Android-chunked file past ~4 GB) —
moves the chunk list into its own encrypted blob and the record carries the pointer (`ps`).
Transfers are unchanged: content-addressed encrypted blobs, chunked past one platform chunk,
`.part` files verified then renamed, four small files in flight at once, big ones one at a time.

## If a device loses its mapping

Where a folder lives is per device, so that mapping is local — and after a reinstall, an app update
that moves the app's storage, or on a machine you have not set up yet, it is simply not there. That
used to read as *"No folders syncing under this account yet"*, about an account syncing two folders.

Folder Sync now asks the account what it syncs and lists anything this device has no directory for
under **Synced on your other devices**. Point it at a folder and it rejoins the same pair — no name to
type, and nothing re-uploaded: the first sweep finds the same bytes on both sides and just records
that they agree.

## When it runs

Uploading holds a phone's radio awake far longer than the bytes suggest, so the policy answers *how
much* rather than yes/no: a full rehash is a plugged-in job, changed files only is the battery case,
and below the battery floor it notes what changed and uploads later. "Sync now" overrides all of it —
refusing someone standing there because the battery is at 19% is how a feature earns a reputation.

**On Android it syncs in the background on its own, and nothing has to be switched on for it.** Add a
folder and the app arms its own alarm; every sixteen minutes the phone wakes, checks your settings,
and sweeps if there is anything to do. Your "only when plugged in" and "Wi-Fi only" settings still
gate whether anything runs, and a folder you have never pressed Start on stays stopped.

That sentence was not true until recently, and the reason is worth stating because it made every
earlier fix invisible. The clock used to live inside **"Stay connected"** — a *notifications* feature,
off by default, for receiving DMs and calls where no push distributor is installed. So on a phone that
had never turned that switch on there was no clock at all: the alarm that fires in Doze, the wake
lock, its renewal and finally a whole sweep engine written in Java were all downstream of a tick
nothing emitted. Folder sync worked while the screen was on, because the page's own heartbeat ran, and
stopped when it went off. Reported exactly that way, on a phone and a tablet, more than once.

Three things make it work now, and each one was separately missing:

* **Its own clock.** An `AlarmManager` alarm that fires in Doze — **not** a `Handler`, whose delays
  are measured on `uptimeMillis()` and stop advancing in deep sleep, which is precisely the state
  this exists for. Armed by folder sync itself whenever this device syncs anything, re-armed after a
  reboot (an alarm does not survive one), cancelled when the account syncs nothing. It asks for an
  **exact** alarm where the platform will give one, which sounds like a detail about punctuality and
  is the thing that decides the next bullet: Android 12+ only lets an *exact* alarm start a
  foreground service in the background.
* **Somewhere to run that Android will not freeze.** A wake lock keeps the *processor* awake and
  does nothing about the *process*: a few seconds after the alarm fires the app is cached, and a
  cached process on modern Android is **frozen** — threads stop, transfers stall, nothing is logged.
  That is what "it runs for a moment after the screen goes off and then stops" actually was. So the
  sweep runs in a **foreground service**, where every other sync app on Android runs, joining the
  app's single background notification rather than adding a second one — and when Android refuses to
  start one (on 13+ the exact-alarm permission is the user's to grant, so that is the *ordinary*
  case) it falls back to an **expedited background job**, which carries no such restriction and also
  keeps the process out of the freezer. The service has no time limit and a job is capped near ten
  minutes, which is why the service is tried first. Folder Sync → **Background details** says which
  one this phone actually got.
* **The key, put where the sweep can reach it.** Every network step is signed by your Nostr key, and
  the only two things that ever stored one on the device were the "Sign for other apps on this
  phone" switch and pairing a laptop over NIP-46 — neither of which has anything to do with syncing
  a folder. So the sweep declined with *"the account key is not on this device"* about a key the app
  was holding. Folder sync now asks for it itself, sealed in the Android keystore, and **does not**
  expose the phone to other apps as a signer. It is asked for only once this device actually syncs a
  folder, it is re-armed when you switch accounts (an unattended sweep signing as the *previous*
  account is a 403 at best), and **signing out takes it off the phone**.

**And one line under all of it decided the whole thing.** The sweep asks the phone whether it is
online, and it used to read "I could not determine the network" as "there is no network" — which is
precisely what a *dozing* device answers, and dozing is the state this entire feature exists for. So
it declined every background tick as offline: it worked whenever the app was open and never with the
screen off. An unreadable network is now treated as unknown rather than as a no; a device that really
has no network still says so.

**The sweep is Java, not JavaScript,** for the same class of reason: Chromium throttles a hidden
page's JavaScript however awake the processor is, so the alarm could fire perfectly and a JS sweep
still would not run.

**The unattended `SyncCheckWorker` can still only notice, not upload,** and that is unchanged: it
walks the tree while charging on Wi-Fi, hashes nothing, holds no key and opens no socket, and *tells
you* there is something to sync. It exists for the accounts the sweep above cannot serve.

It **moves bytes; anything that needs a decision waits for you**. A folder's *first* sync is deferred
until you open the app — it hashes everything and publishes the folder's whole contents, which is the
worst thing to start unattended. So are conflicts. And a sweep that would empty a folder, or refill
one from a machine whose timestamps moved, refuses rather than asking a phone nobody is looking at —
suppressing only the deletions (or only the resurrections), never the rest of the sweep.

The *file* key it is handed is **already wrapped**: the same NIP-44-sealed value your encrypted drive
publishes, which only your Nostr key opens. So nothing new is written to the phone in the clear, and
**an account signed in through Amber or a remote signer has no key here at all** — on those, the
alarm still wakes the app and asks it to sync, exactly as before, because nothing on the device can
sign an upload unattended.

Folder Sync → **Background details** copies out what the phone measured, including what the native
sweep decided and did last — "no key on this device", "first sync — open the app once", the counts.

## Browsing a folder on a device that does not sync it

Files → **Blossom** lists your synced folders beside the drive's own folders, and you can walk into
one from any device, including a browser that cannot sync at all. The rows come from the manifest —
what your devices agreed the folder contains — and a download decrypts in your browser.

You can also **add, rename and delete** from there, from a device that holds none of the files. What
those edit is the **manifest**, never a file: the devices carry the change out on their next sweep,
through the paths they already use — a new entry is downloaded, a deletion is moved to `.pc-trash`,
a rename is both. Nothing is erased, and a rename moves no bytes (the blob is already stored; only
the name in the agreement changes).

Because a delete here happens without looking at any of the files, it is the one action on that
screen that names its consequence first: it counts the files, says that every device moves its copy
to `.pc-trash`, and the server's collapse guard — the same one that refuses a manifest that shrinks
sharply — still stands behind the write.

It is the **same delete** a device makes, with the same known edge: a machine whose agreement was
cleared (a reinstall, "Stop syncing" and back) and that still holds the file will put it back, because
[delete loses to edit](#safety-rules) and with no `base` both sides look
changed. Delete it again from a device that is up to date, or let that machine sync once before you
tidy up. The simulation runs all three arms — deleted on a device, deleted from the browser, key
dropped instead of tombstoned — and requires one outcome. Uploads go up encrypted, content-addressed and chunked
above 16 MB, exactly as a sweep's do, and a batch writes the shared list once per twenty files rather
than once per file. Dropping a whole *folder* onto the screen is deliberately not supported, because a
half-walked directory tree would be a half-created folder on every device.

Two things it will refuse outright, both because a manifest has no folders in it and your disks do:
adding a file whose name is already a folder here (or putting something *inside* a name that is a
file), and deleting a folder whose contents grew while the screen was open — the count you were shown
is the count it will delete, and if another device changed it in between you are asked again.

There are no thumbnails for the same reason there is no server-side search: every blob is ciphertext,
so a preview costs a full download and a decrypt per file, and a folder of 4000 photos would pay that
4000 times to draw one screen.

## Where it lives

| Piece | File |
|---|---|
| The reconciler (pure: merge the devices' records → a plan → check it) | `static/js/client/syncengine.js` |
| The executor (moves bytes; decides nothing) | `static/js/client/syncexec.js` |
| Content identity, exclusions, naming, the battery policy | `static/js/client/foldersync.js` (+ `syncrun.js` for `due()`) |
| Store, scheduler, the screen | `static/js/client/sync.js` |
| Desktop adapter | `desktop/fsbridge.js` (+ `main.js` IPC, `preload.js` → `window.pcFs`) |
| Android adapter | `FolderSyncPlugin.java` + `static/js/client/fs-android.js` |
| The same rules again, for screen-off sweeps | `SyncReconcile.java` + `NativeSweep.java` |
| Read/write a device's record | `POST /client/sync-manifest` (`app/routers/client.py`) |
| The account's folder list | `POST /client/sync-folders` |

The split is load-bearing: everything that can get the *answer* wrong is pure and tested, and
everything that can destroy a *file* is a thin adapter. The reconciler cannot touch a file; the
executor cannot be reached without a plan that has been checked.

The records go through the server rather than straight to the relay because the server holds the
account's storage key and is where the write is signed. It no longer needs to guard them: a document
with one writer for ever cannot be clobbered by a stale reader, which is what the old shrink guard
existed to prevent.

The rules exist twice — once in JavaScript, once in Java — because a hidden WebView's JavaScript is
throttled to about one timer a minute, so a sweep that runs while the phone is asleep cannot be the
JS one. `tests/test_android_reconcile_parity.py` runs both over hundreds of generated folder states
and compares the plans decision for decision.

## Tests

```
node tests/client/engine_sim.js                                        # merge, concurrency, the state table, the guards
node tests/client/exec_sim.js                                          # 20 end-to-end scenarios, real bytes
venv-unified/bin/python -m pytest tests/client/test_folder_sync.py     # the rules and the battery policy
venv-unified/bin/python -m pytest tests/client/test_fs_bridge.py       # the real desktop bridge, real files
venv-unified/bin/python -m pytest tests/client/test_sync_store_scale.py # a 15790-file folder's record
venv-unified/bin/python -m pytest tests/test_android_reconcile_parity.py # JS and Java decide identically
venv-unified/bin/python -m pytest tests/test_sync_folders_listing.py   # the account's folder list
venv-unified/bin/python scripts/check_files_explorer.py                # the Files layout, phone + desktop
```

`exec_sim.js` is the one to keep honest. Every scenario in it is named after the report that produced
it, it drives the **shipped** chunker lifted out of `app.js` rather than a copy, and the files carry
real content — a wrong offset, a dropped chunk or a truncation changes a hash and fails the run. It
covers three devices writing at once, a device that cannot be read, an emptied store, a lost folder
handle, a reinstall, an interrupted sweep resuming, corruption on both transfer paths, a device whose
own record was lost, and 6,000 files.

What no simulation covers: the signing, the real Blossom round trip and the platform adapters on a
real phone. A desktop → phone trip is still the only thing that exercises those together.
