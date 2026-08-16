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
`tests/client/two_device_sim.js` now runs two independent devices against one manifest so that
cannot come back silently.

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
  sweep. It is reported by name, not as a tally: deciding whether to delete them again is a decision
  you can only make if you can see which files they are.
- **A sweep may not delete more than it keeps.** Every rule above decides one path, and each of them
  is right — but a manifest that has gone wrong does not produce one bad decision, it produces ten
  thousand identical ones. Measured on a real Pictures folder: the shared manifest held ~10k paths
  and every single one was a tombstone (`n=0` live on the server), so re-adding the folder on the
  device that still had the files read "deleted elsewhere" for all of them and moved the lot into
  `.pc-trash`, correctly, per path, without a word. Past 20 files, a sweep that would trash more
  than survives it stops and asks; a background sweep — the watcher, a resume, the heartbeat — has
  nobody to ask and so refuses. A refusal is **not recorded in the agreement**, so the next sweep
  re-proposes it and asks again, and only deletions are suppressed: uploads and downloads still run,
  because a guard that aborts the whole sweep turns "it deleted everything" into "it syncs nothing,
  for ever". The server's collapse guard cannot cover this — a mass *local* delete writes no
  manifest at all, it only advances `base`.

## Size, and why a big folder used to be unsyncable

A folder of 15790 files uploaded everything, then resynced from the first file — over and over. Three
separate ceilings, all of them silent, all now handled:

- **The manifest.** NIP-44 refuses a plaintext over 65535 bytes and a manifest entry measures ~174,
  so the document held about **376 files**. Past that `save()` threw at the very last step of every
  sweep: the uploads had happened, the manifest was never stored, the agreement was never written,
  and the next sweep read the whole folder as new. Everything except the final step worked, which is
  precisely why it looked like a working sync. Past 45 KB the paths now move into an encrypted
  Blossom blob and the document keeps a pointer — the same thing the files index does, for the same
  reason. `sealed` is still set, to a deliberately undecryptable marker, so a client older than this
  change **fails** instead of reading the document as an empty folder and trashing everything in it.
- **The agreement.** `base` is the same size as the manifest (~2.6 MB for that folder) and lived in
  localStorage under a `catch` that swallowed the quota error. It is in IndexedDB now, and a write
  that fails is reported rather than swallowed — a base that does not persist is the same infinite
  resync from a different cause.
- **Interruption.** `base` advanced per file in memory but was written once, at the end, so a sweep
  that never reached the end recorded nothing and the next began at file one. On a big folder that is
  not a slow resume, it is a folder that can never finish on a machine anyone ever closes. Progress
  is checkpointed during the sweep now, at most 20 times per sweep however large the folder is —
  each checkpoint rewrites the whole manifest, so they are bounded on purpose.
- **Big files are chunked, on every platform.** Past 16 MB a file goes up in pieces rather than
  whole, because the whole-file path holds the plaintext, the ciphertext and the upload body at once
  — three to four times the file — and that killed the desktop renderer and the Android WebView
  alike. Each chunk is content-addressed and checked before it is sent, so an interrupted transfer
  resumes, appending to a file re-sends only the new part, and no single request exceeds a chunk
  (which is also what makes this work behind a proxy that refuses bodies over ~95 MB).
- **Superseded manifest blobs.** Each save past that threshold uploads a whole new encrypted blob, so
  a long first sync leaves one per checkpoint. The document carries a one-deep chain and the server
  lets go of the generation behind it once the replacement is safely stored — ownership-checked, so
  bytes another account also references are never touched, and then on a week's TTL rather than
  deleted outright, so letting go of the wrong thing stays recoverable. (`keep` blobs are exempt from
  the admin's blanket age TTL, which is what stops turning that setting on from eating an encrypted
  drive; an expiry stamped on one proven-unreferenced blob is honoured.)
- **A deliberate mass delete.** Deleting most of a folder trips the server's collapse guard, and a
  refused save meant the agreement was never written and every later sweep proposed the same delete
  and was refused again — the delete could never land. The sweep knows how many paths it removed, so
  when that accounts for the shrink it re-sends with `force`; when it does not, it asks, and honours
  a no.

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
| The decision engine (pure: three snapshots → a plan) | `static/js/client/foldersync.js` |
| The executor (what order, and what to do when a step fails) | `static/js/client/syncrun.js` |
| Store, scheduler, the screen | `static/js/client/sync.js` |
| Desktop adapter | `desktop/fsbridge.js` (+ `main.js` IPC, `preload.js` → `window.pcFs`) |
| Android adapter | `FolderSyncPlugin.java` + `static/js/client/fs-android.js` |
| The manifest, and the collapse guard | `POST /client/sync-manifest` (`app/routers/client.py`) |
| The account's folder list | `POST /client/sync-folders` |

The split is load-bearing: everything that can get the *answer* wrong is pure and tested, and
everything that can destroy a *file* is a thin adapter.

The manifest goes through the server, which is a deliberate exception to "prefer Nostr over a server".
It is the only record of what every device agreed a folder contains, so an empty read written back
over a full one does not lose a setting — it loses the folder, because every other device then reads
the missing paths as "deleted elsewhere" and trashes its copies. The collapse guard refuses that write
server-side, where no client build can route around it.

## Tests

```
venv-unified/bin/python -m unittest tests.client.test_folder_sync      # the merge, thousands of scenarios
venv-unified/bin/python -m unittest tests.client.test_sync_run         # the ordering rules
venv-unified/bin/python -m unittest tests.client.test_two_device_sync  # two devices, one manifest
venv-unified/bin/python -m unittest tests.client.test_sync_store_scale # a 15790-file folder's store
venv-unified/bin/python -m unittest tests.test_sync_folders_endpoint   # the account's folder list
venv-unified/bin/python scripts/check_files_explorer.py                # the Files layout, phone + desktop
```

`test_two_device_sync` is the one to keep honest: it runs two (and in one scenario three) independent
devices with their own filesystems, their own agreement and their own platform ids, against one shared
manifest. It does **not** exercise the platform adapters, the signing or the real Blossom round trip —
a real desktop → phone trip is still the only thing that covers those.
