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

- **Superseded manifest blobs.** Each save past that threshold uploads a whole new encrypted blob, so
  a long first sync leaves one per checkpoint. They are `keep` blobs and the cleanup sweep skips
  those unconditionally — the exemption that stops an admin turning on a TTL from eating an encrypted
  drive — so nothing else would ever collect them. The document carries a one-deep chain and the
  server releases the generation behind it, ownership-checked, once the replacement is safely stored.
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

**Background sync cannot upload, and that is not a bug to fix.** Every network step is signed by your
Nostr key, and with Amber or a remote signer that key is not on the device. Android's background job
therefore walks the tree, hashes nothing, and *tells you* when there is something to sync — opening
the app is what syncs.

## Browsing a folder on a device that does not sync it

Files → **Blossom** lists your synced folders beside the drive's own folders, and you can walk into
one from any device, including a browser that cannot sync at all. The rows come from the manifest —
what your devices agreed the folder contains — and a download decrypts in your browser.

It is **read only** there, deliberately. Renaming or deleting from that screen would be a write to the
shared manifest that every other device then applies to real files on real disks, which is the sync
engine's job, with its three snapshots and its trash, not a file browser's.

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
