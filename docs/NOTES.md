# Notes

Private, end-to-end encrypted, offline-first note taking, in the Nostr client at `/client` →
**Notes** (desktop sidebar, or ☰ **More** on a phone).

Nothing about a note is readable by the server. Each note is encrypted to your own key before it is
published, so the operator, the relay and a database dump all see the same thing: ciphertext. The
deliberate consequence is that the server can't offer notes anywhere it would have to read them —
there is no `notes` chat command, nothing on Telegram, and the AI cannot search them.

## Where a note lives

One Nostr event per note: kind `30078`, `d = pcai:note:<id>`, content NIP-44-encrypted to your own
pubkey. Folders are the same shape (`pcai:notefolder:<id>`). Both carry `l = pcai-notes`, which the
relay indexes, so the whole library loads as one filtered subscription instead of a scan of every
document you own.

There is no index document anywhere, on purpose. An index is a second source of truth that one
empty read can wipe — the failure that has already cost this app a follows list and a drive's file
index. The library is derived by querying, every time.

**Per note, not one document.** Budget keeps its whole dataset in a single event; Notes must not.
A single document is a read-modify-write of everything on every save, so two devices editing
different notes lose one of them, and a Joplin library of a few thousand notes does not fit in one
event at all. Per note: each note is its own conflict domain, a sync is a delta, and the blast
radius of any failure is one note.

**What is still public:** the `d` tag and the event timestamps. Someone watching the relay learns
how many notes you have and when you write them, never what is in them. Note ids are therefore
random, never a slug of the title.

## Offline

Reads come from the local relay cache first, which persists to IndexedDB, so every note you have
synced is readable with no network at all — including in the Electron app and the APK.

Writes are the harder half. The app's Outbox deliberately **refuses** replaceable kinds, because
replaying a stale replaceable event is exactly how the follows list got wiped once. Notes therefore
carries its own small queue (`pcaiNotesPending`), which is safe for reasons the generic one can't
assume:

* what's queued is the already-signed, already-encrypted event, so it leaks nothing extra on disk;
* a note is a self-contained document, not a membership list, so re-sending one can only restore
  that note — it cannot silently erase items the client never knew about;
* on flush, a queued edit is **discarded** if the library already holds a newer version of that
  note (another device won the race). Publishing it would resurrect an older body over a newer one.

The queue drains on `online` and on a periodic check. Until it does, the editor says *"saved on this
device — will sync"*, and the sidebar shows how many are waiting. A note typed on a train is in the
local cache the moment you stop typing; `scripts/check_notes_mobile.py` asserts exactly that, because
`publish()` rolls its optimistic cache save **back** when the relay refuses, and that rollback is
what would otherwise eat it.

## Attachments

Encrypted with the client's master key and stored on Blossom through the same path as encrypted
music — one encrypted drive, not two. The note holds only the sha256; the bytes are useless without
the key, which never leaves your device.

**They are cached on the device, like everything else.** A blob is addressed by the sha256 of its own
bytes, so the service worker stores it cache-first in its own cache (`pc-drive-v1`, `sw.js`) and a
second view of a note costs no network at all. Its own cache and not the media one, deliberately: the
timeline's images are a firehose that would evict a deliberately imported library within a session.
The rule matches "the last path segment is a 64-hex hash", **not** `/blossom/…` — `encFileUrl` fetches
`mediaServer() + '/' + sha`, and that is the user's own server root whenever they have set one, so a
path-anchored rule would have silently cached nothing for exactly those people
(`tests/test_sw_video_routing.py`).

**And they load as you reach them, not all at once.** Every `pcres:` picture in a note is a full
download of the ciphertext plus a decrypt, and an imported note is routinely dozens of screenshots:
resolving them on open fired 131 requests in eleven seconds and left the note looking broken until
they landed. Pictures are now loaded by an IntersectionObserver through a queue four wide, links
resolve on click rather than on open, and the attachment strip lists 12 with a "show N more" — a
thumbnail is a private download too. A picture that fails **stays a picture**: it keeps its element,
marks itself, retries once on its own and again on tap. It used to be replaced by a permanent
"[image unavailable]", so one dropped request looked exactly like a lost attachment.

Those uploads set `X-Keep`, which exempts the blob from the server's age sweep **forever**
(`blossom_service._cleanup_once`). This matters: the sweep is driven live by
`blossom_blob_ttl_days`, so turning that setting on a year from now would otherwise retroactively
delete every attachment, every music track and the files index — ciphertext the node holds the only
copy of, whose loss nobody could notice until they opened a note and the picture was gone. The relay
side has the matching guard: kind 30078 is exempt from the NIP-40 expiration sweep
(`_NEVER_EXPIRE_KINDS`), so a stray `expiration` tag cannot delete a note. NIP-37 *recommends*
stamping drafts with `expiration: now + 90 days`, so without that exemption a note touched by any
other client following that convention would quietly disappear three months later.

## Importing from Joplin

**Notes → Import**, then either:

* **a `.jex` file** — Joplin's own export: *File → Export → JEX*. This is the supported path.
* **a folder of `.md` files** — Joplin's "Markdown + Front Matter" export.

The import runs entirely in your browser. Notes are encrypted with your key before anything is
published, and attachments are encrypted before upload, so the server never sees a note or a file
in the clear. It cannot be a server-side script for that reason: only the browser holds the key.

What comes across: notebooks (as folders, with their full path as the name), tags, to-dos (as a
`todo`/`done` tag), conflicts (as a `conflict` tag), attachments, and the `:/id` links in note
bodies rewritten to point at the imported attachments. Note-to-note links are left as written.

**Re-running an import updates rather than duplicates.** Every imported note keeps its Joplin id, so
a second run of the same export matches and overwrites. That is also the recovery path: an import of
a few thousand notes will be interrupted at some point — close the tab and run it again.

### If it goes wrong

* *"no Joplin notes found in that file"* — that isn't a `.jex`. A `.jex` is a tar archive of
  `<id>.md` files; a `.zip` of Markdown is the *other* export, so use the folder option.
* *"N item(s) in this export are still ENCRYPTED by Joplin"* — the export was made with Joplin's
  end-to-end encryption on, so it contains ciphertext and empty titles. Importing it would produce a
  wall of blank notes. Disable E2EE in Joplin (or wait for it to finish decrypting), export again.
* *"N attachment(s) had no file in the export"* — the export was made without resources, or Joplin
  never synced those files down on that device. The notes still import; only the files are missing.
* Some attachments failed to upload — the notes still imported with their original link text. Run
  the import again; it will retry them and update the notes in place.

The importer is `static/js/client/joplin.js`, deliberately DOM-free so it can be tested directly:
`tests/test_joplin_import.py` builds real `.jex` archives with Python's `tarfile` and runs the
shipped parser under node against them.

**The first attempt at this feature (`scripts/migrate_joplin.py`, removed) read Joplin's live
`database.sqlite` from a server-side script.** It only ran on the machine Joplin was installed on,
broke whenever Joplin migrated its schema, read nothing at all when the user had E2EE on, and wrote
into a server-side SQL `Note` model that no longer exists. Don't go back to that shape.

### One parsing rule worth knowing

A Joplin `.md` item is `title`, blank line, `body`, blank line, then `key: value` metadata. The only
correct way to split it is to walk **backwards** from the last line, taking properties until a blank
line. Any forward scan for the metadata block breaks on a note whose body contains a line like
`todo: call the bank` — which is normal prose, not a corner case. `parseItem` is a faithful port of
Joplin's own `BaseItem.unserialize` for this reason; there is a test for exactly that body.

## On a phone

The list **is** the page. Its toolbar carries the current folder, the count, search and New note;
the folder tree lives behind the folder button as a drawer, and picking anything closes it (Escape,
the backdrop and the Android back button all close it too).

It shipped as a pane stacked above the list, capped at `40vh` — so folders held half the screen at
all times and the notes got the remainder, measured at **273px of 726**. Above 820px nothing changes:
the sidebar is still a column and the same markup serves both, because a second copy for phones is
how the two quietly diverge.

## Checks

    venv-unified/bin/python -m pytest tests/test_joplin_import.py tests/test_blossom_keep.py tests/test_relay_prune.py
    venv-unified/bin/python -m unittest tests.test_sw_video_routing
    venv-unified/bin/python scripts/check_notes_mobile.py

`check_notes_mobile.py` drives the real `notes.js` against a stubbed host at phone AND desktop widths
and asserts the layout collapses to one pane with a way back, that no input is under 16px (iOS zooms
the page on focus and never zooms out), that nothing sits under the fixed bottom nav, that the folder
tree is a drawer rather than a pane and can be opened and closed, that opening a picture-heavy note
does not fetch every picture in it, that a failed picture stays a retryable picture, that the
attachment strip doesn't crush the note's text — and that the offline write survives. Run it before
deploying a Notes change; `check_client_mobile.py` never opens this screen.

Each assertion was verified to FAIL against the behaviour it replaced, which is the only way to know
a guard guards anything.
