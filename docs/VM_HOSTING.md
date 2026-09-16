# VM hosting (Virtual Machines)

PosterChan's own Proxmox-like VM hosting. A PosterChan **server node** with libvirt runs virtual
machines; admins create, delete and assign them; the people they are assigned to start, stop, reboot
and open their console — from the **Virtual Machines** screen of any PosterChan client (web, Android,
desktop, even a bundle with only a Nostr key and relays).

Management is **encrypted Nostr**, not an HTTP API: every operation is a signed kind-5310 event,
NIP-44-encrypted to the host node's key. The only thing that uses the node's HTTPS is the console
(noVNC), and it needs a one-use ticket that was itself issued over Nostr.

**Phase 1**: host info, VM list/get, power, create from a library ISO, delete, assign/unassign,
console. **Phase 2**: hardware settings, snapshots, the ISO library (download by URL, upload), access
management from the client, session keys for remote signers, host discovery, and "This computer" on
the desktop app. **Phase 3**: cold migration between hosts (§6).

---

## 1. Set up a host

### Packages

| Distro | Packages |
|---|---|
| Gentoo | `app-emulation/libvirt[qemu,virt-network]`, `app-emulation/qemu`, `sys-firmware/edk2-bin`, `app-crypt/swtpm` |
| Debian/Ubuntu | `libvirt-daemon-system libvirt-clients qemu-system-x86 qemu-utils ovmf swtpm-tools` |

Then:

```sh
sudo systemctl enable --now libvirtd
sudo virsh net-autostart default && sudo virsh net-start default   # the NAT network VMs attach to
ls -l /dev/kvm                                                       # hardware virtualisation present?
```

### The app user

```sh
sudo usermod -aG libvirt,kvm <the user posterchanai.service runs as>
sudo systemctl restart posterchanai.service       # group changes need a new process
```

> **Security: membership of `libvirt` is effectively root on this machine.** Anyone who can drive
> `qemu:///system` can define a VM that mounts `/` from the host. Enabling VM hosting means trusting
> this app — and therefore every admin npub you list — with that. Keep the admin list short.

### Storage

```sh
sudo mkdir -p /var/lib/posterchan/vms/isos
sudo chown -R <app user>:libvirt /var/lib/posterchan/vms
sudo chmod 2750 /var/lib/posterchan/vms
```

Layout, all built by the server from ids — **clients never send a path**:

```
/var/lib/posterchan/vms/<vm-uuid>/disk-vda.qcow2   the disk
/var/lib/posterchan/vms/<vm-uuid>/nvram.fd         EFI variables
/var/lib/posterchan/vms/<vm-uuid>/domain.xml       the definition handed to libvirt (recovery copy)
/var/lib/posterchan/vms/isos/*.iso                 the ISO library (copy installers here)
/var/lib/posterchan/vms/<vm-uuid>/disk-vdb.qcow2   a disk added later in VM Settings (vdb…vdz; sdb… on Windows)
/var/lib/posterchan/vms/isos/.incoming/*.part      downloads/uploads until they are whole (never listed)
/var/lib/posterchan/vms/.state/                    lock + op journal + sessions.json (0600)
```

An ISO id is a bare file name, and its RESOLVED path must still be a regular file inside `isos/` —
a symlink pointing out of the library is refused, not followed. libvirt's `qemu` user needs to read
the ISOs and write the VM directories (on Debian with AppArmor/`dynamic_ownership` this is automatic;
otherwise make the directory group-accessible to `libvirt-qemu`/`qemu`).

### Turn it on

Admin → **VMs** (System group):

- **Run the VM host on this server** — `vmhost_enabled`.
- **Public HTTPS URL** — `vmhost_public_url`, e.g. `https://poster.place`. Blank = the console is
  opened on the same origin as the relay the client reached the host through.
- **Extra admin npubs / Allowed user npubs** — `vmhost_admin_npubs` / `vmhost_allowed_npubs`.
- Limits, reserves, the libvirt URI, the network or bridge.
- **Check VM host status** shows whether it is running, its npub, libvirt reachability, KVM and
  whether the storage directory is writable.

Every setting is stored in Nostr (operator-signed, NIP-44 `pcai:setting:` documents) like the rest of
Admin. The access lists and the on/off switch are saved **durably** — Save reports an error (503)
rather than success if the relay did not take them, because a lost write reads back as the old list
after a restart, i.e. a revoked npub regaining access.

The host runs inside the port-3051 app process and takes an `fcntl` lock on
`<storage>/.state/lock`, so a second process pointed at the same storage refuses to start.

### nginx

The console socket is `/ws/vmconsole`, already covered by the stock `location ^~ /ws/ { … }` block
(3600s timeouts). A host that will be a migration SOURCE also needs the transfer location (shipped in
`nginx/posterchanai.conf.example` and `docker/proxy/posterchanai.conf`):

```nginx
location ^~ /api/vmhost/transfer/ {
    proxy_pass http://posterchanai_app;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

It must come before `location ^~ /api/`. Don't add a `proxy_set_header` to either — see
`docs/NGINX.md`.

---

## 2. Who can do what

| Role | Who | Can |
|---|---|---|
| **admin** | any `User.is_admin` account's linked npub, `vmhost_admin_npubs`, and this node's own operator key | everything: host capacity, all VMs, create, delete, assign, console |
| **user** | `vmhost_allowed_npubs`, plus every npub some VM is assigned to (the assignment is the grant) | see ONLY their assigned VMs; start, shut down, reboot, force off; open the console |
| a **session key** | a throwaway key a user or admin opened with `session.open` | the same as its owner, but ONLY `host.whoami/info`, `vm.list/get/power`, `console.ticket`, `session.close`; anything else is `step_up_required` |
| anyone else | — | nothing. Their requests are **dropped silently** — no reply at all |

A user asking about a VM that is not theirs gets `not_found`, never `forbidden` (which would confirm it
exists). Unassigning somebody closes their open console immediately.

Everything that changes a VM or the host — create, delete, assign, **settings, snapshots, ISO
download/upload/delete, access** — is admin-only, and that is enforced in the host's op table
(`service.OPS`), never by the client hiding a button.

**Access from the client** (`host.access.get/set`, the host screen's **Access**): `set` writes ONLY
`vmhost_allowed_npubs`. The admin list is shown read-only and changed in the site's Admin → VMs,
behind a site login — a key that can sign as one host admin must not be able to mint more admins over
Nostr. Every entry must be an npub or 64-hex key; one bad entry refuses the WHOLE request (a pasted
`nsec` is refused and never echoed). The write goes through `settings_store.write_through` and must
land before the running host changes or success is reported.

---

## 3. Wire protocol (v1)

| Kind | What | Tags |
|---|---|---|
| 5310 | request, client → host | `p` host, `expiration` (≤ now+120), `nofederate` |
| 6310 | result, host → client | `e` request, `p` requester, `nofederate`, `expiration` (+300) |
| 7310 | progress (zero or more before the result) | as 6310, `expiration` +600 |
| 31310 | host announcement (public, addressable) | `d=posterchan-vmhost`, `relay` |

Content is NIP-44 (requester ↔ host). Request plaintext (≤ 65 KB):

```json
{"v":1, "id":"<idempotency id>", "op":"vm.power", "ts":1726500000, "args":{"vm":"<uuid>","action":"start"}}
```

Result: `{"v":1,"id":…,"ok":true,"result":{…}}` or `{"v":1,"id":…,"ok":false,"error":{"code","message"}}`.
Progress: `{"v":1,"id":…,"progress":{"phase","msg"}}`.

Ops: `host.whoami`, `host.info` (capacity for admins only), `vm.list {cursor,limit≤50}`, `vm.get {vm}`,
`vm.power {vm, action: start|shutdown|reboot|destroy}`, `console.ticket {vm}`, and admin-only
`iso.list`, `vm.create {name, guest: linux|windows, firmware: efi|bios, vcpus, ram_mib, disk_gib, iso?,
autostart, start}`, `vm.delete {vm, confirm_name, delete_disks}`, `vm.assign/vm.unassign {vm, pubkey}`.

Phase 2 ops (all admin except the session ops):

| op | args | notes |
|---|---|---|
| `vm.update` | `vm`, any of `vcpus`, `ram_mib`, `autostart`, `boot: disk/cdrom`, `add_disk_gib`, `add_nic: true`, `media: {iso} or "eject"`, `input: tablet/mouse` | VM must be **shut off**; validated + capacity-checked before any write; ONE redefine; read back and compared — a field the host did not keep is `backend_error`. A cdrom's source is REPLACED (`change-media --update` semantics), never a second one added. A failed define removes the disk it just created. Result carries `vm.hardware`. |
| `vm.get` | `vm` | admins also get `hardware {boot, input, nics, disks, media, cdrom}` |
| `vm.snapshot.list/create/delete` | `vm`, `name` (≤48, `[A-Za-z0-9_.-]`), `description?` | libvirt internal snapshots, ≤32 per VM |
| `vm.snapshot.revert` | `vm`, `name`, `confirm: true` | without `confirm` → `bad_request`; closes consoles |
| `iso.list` | — | now also `jobs` (downloads in progress) and `fetch_enabled` |
| `iso.fetch` | `url`, `name?` | see below; 7310 progress every 2 s; the result arrives when the download is whole |
| `iso.upload_ticket` | `name`, `size` (bytes) | → `{ticket, url, name, size, exp}`; 10 min, single use |
| `iso.delete` | `iso` | `conflict` while any VM's metadata or cdrom still uses it |
| `host.access.get/set` | `set`: `allowed: [npub or hex]` | see §2 |
| `session.open` | `pk`, `exp`, `scope: "use"`, `proof` | user; must be the REAL key |
| `session.close` | `pk?` | a session closes itself; a real key closes one or all of its own |

A snapshot/update on a VM a migration holds is refused `migrating` — the same guard as start/delete/assign:
the migration journal first (a migration still quiescing/exporting has no metadata tag yet), then the
`pc:migration` tag on the VM (which survives a lost journal).

Error codes: `bad_request forbidden not_found conflict busy insufficient_capacity unsupported
rate_limited backend_error timeout version internal migrating session_expired step_up_required aborted`.

A request signed by a SESSION key may run only `host.whoami host.info vm.list vm.get vm.power
console.ticket session.close`; everything else — `vm.migrate.*` included — answers `step_up_required`.
`peer.migrate.*` needs the peer role (a key in `vmhost_peer_hosts`), which a session can never carry.

**Client correlation:** sign the request, SUBSCRIBE to `{kinds:[6310,7310], authors:[host], #e:[id]}`,
THEN publish. A result counts only if it verifies, is authored by the host, e-tags the request and
carries the same `id`.

**Replay and retries.** The host drops an event older than 120 s, more than 30 s in the future,
without an expiration, expired, or already seen. "Seen" is kept by TIME for the whole clock window
(never evicted by count, so a flood cannot push a victim's id out — at its hard cap new requests are
refused instead), for the life of the process across host-service restarts, and in
`.state/seen.log` across process restarts; the subscription itself asks only for the last 120 s. So
the requests a relay replays to a restarted host (it stores them until they expire) are not handled a
second time — no re-issued console ticket, no re-run of a refused operation. A client that heard nothing retries with a NEW event
carrying the SAME `id`; for mutating ops the host's journal (`.state/journal/ops.jsonl`, 15 min)
returns the stored result instead of powering the VM twice. Only successes are journaled — a refusal
(busy, capacity) lets the retry actually retry.

**Relay carriage.** The requester is usually outside the relay's web of trust. The relay's write gate
and firehose (`app/services/vmhost/kinds.py`) apply special rules to NON-MEMBERS only — a web-of-trust
member's event of these kinds is treated exactly as it was before VM hosting (other apps use the
numbers too). A non-member's:

| Kind | Accepted only when |
|---|---|
| 5310 | this node runs a VM host (`vmhost_enabled`, applied live), the `p` tags are exactly `[this node]`, expiration ≤ now+600, `nofederate`, ≤ 70 KB |
| 6310 / 7310 | authored by this node's key or a configured peer host (`vmhost_peer_hosts`, read by the relay only while `vmhost_enabled`), with an expiration and `nofederate` |
| 31310 | authored by this node, a peer host or an operator |

Turning the host on adds a firehose subscription for 5310/6310/7310 `#p`-tagged to the node; switching
it and the rules above on or off is applied live via reload-upstream, no relay restart.

**Load limits.** A requester's pubkey has a token bucket (users: burst 10, 1/s; admins: burst 30, 3/s)
spent before anything is decrypted — and only after the signature verifies, so a forgery wearing
somebody's pubkey cannot drain theirs. Requests past the bucket are dropped without an answer (an
answer would cost a signature per flood event). Executing requests are bounded per role (32 user,
16 admin), so a user flood of slow operations cannot keep an admin out. A request signed by a session
key is charged to the session's OWNER (bucket and slot), so opening sessions buys no extra budget.
Configured PEER HOSTS have their own bucket (burst 60, 5/s) and their own 8 slots — even a peer key that is
also on a user list — so a user flood cannot stall a migration's commit/ack/status mid-handoff (which
would lock both hosts until an admin force-reclaims). The transfer itself is HTTPS and not bucketed. Read ops share one libvirt
listing for 3 s (single-flight; any mutating op invalidates it), and a failed admin-account lookup is
single-flight and cached for 5 s.

**What the encryption does NOT hide (metadata).** NIP-44 hides the operation and its arguments, not
the envelope. Every request is a signed event with the requester's real pubkey as author and a `p` tag
naming the host; every result and progress event names the host as author, the requester in `p` and
the request in `e`; all of them carry `created_at` and `expiration`, and their sizes are visible. So
anyone who can read the relays these events pass through — this node's relay, the upstream relays its
firehose subscribes to, any relay a client publishes to — learns **who uses which VM host, when, how
often, and roughly how much** (a `vm.create` with its progress events looks different from a
`vm.list`). The events are short-lived and `nofederate`, which limits how long and how far that trail
spreads, but does not remove it. Admin npubs and assigned users should treat their use of a host as
public; phase-2 session keys are the planned mitigation for the author half.

**No answer is not "no VMs".** An offline host and a host that drops strangers look identical to a
client; the UI says "No answer — the host is offline or you're not on its list" and keeps the last
known list on screen.

### ISO download (`iso.fetch`) — SSRF by design, so guarded

Downloading a URL on behalf of a client is server-side request forgery unless proven otherwise. The
host uses the repo's guard (`rss_service.looks_fetchable` + `is_safe_host`: http/https only, no
localhost / private / link-local / `.lan` / `.local`, the name RESOLVED before it is trusted) and follows
redirects **by hand, re-checking every hop** — a public URL that 302s to `169.254.169.254` is refused and
the metadata address is never requested (`search_service.fetch_url_content` once got exactly this wrong).
It goes direct, never through the Tor fallback transport. Bytes stream to `isos/.incoming/<random>.part`
with a running SHA-256 and a hard cap of `min(32 GiB, free disk − vmhost_reserve_disk_gib)`, enforced on
the `Content-Length` before reading AND on the stream (a lying or absent header). Only a whole file is
linked into the library, never over an existing name. `vmhost_iso_fetch_enabled` turns it off.

### ISO upload

`iso.upload_ticket` (admin, over Nostr) → `PUT <https>/api/vmhost/iso/<ticket>` with the raw bytes.
The ticket is consumed BEFORE the body is read (so a copy in a proxy log is already spent), re-checks
that its npub is STILL an admin, and the body is streamed to a `.part` and cut off the moment it passes
the declared size; a body shorter or longer than declared is discarded. The route answers CORS `*`
(`/api/vmhost/iso/` is in `_ScopedCORS._OWN_CORS`) because the client usually lives on another origin
and the credential is the ticket, never a cookie. nginx: the stock `client_max_body_size 0` already
allows it; big uploads ride the single uvicorn worker as a stream.

### Session keys (remote signers)

With a NIP-46 bunker, Amber or a browser extension, every request is a round trip to the signer, so a
screen that polls every 10 s either drains it or does not work. The client opens a SESSION per host,
signed by the REAL key:

```
{"op":"session.open","args":{"pk":"<session pubkey>","exp":<unix, ≤ now + vmhost_session_max_hours>,
 "scope":"use","proof":<kind-27310 event signed by the SESSION key,
 content "posterchan-vmhost-session:<owner hex>:<exp>:<host hex>">}}
```

The proof stops anybody registering a key they do not hold (e.g. somebody else's real key, to capture
its traffic); a key that is already an identity on the host is refused. Requests signed by the session
key then act as the owner for use ops only (see §2) and are answered TO the session key. An expired or
closed session is ANSWERED `session_expired` for a day rather than dropped — silence would read as
"host offline". Sessions persist in `.state/sessions.json`, ≤8 per owner. A local-nsec client never
opens one. Client side (`vms.js`): polling ops use the session; everything else, and `session.open`
itself, uses the real key; `session_expired`/`step_up_required` drops the session and retries with the
real key; silence under a session is re-asked with the real key at most every 2 minutes (a host that
lost `sessions.json` must not look dead for ever); a host that refuses `session.open` is not asked again
for 5 minutes.

### Discovery

The host list is the user's `pcai:vmhosts` document (plus this instance's own host). **Add host**
takes an npub (then a relay) or an `nprofile` (whose relay hint is used). **Find hosts** reads 31310
announcements from the pool relays (newest per host), asks each new one `host.whoami` (6 s, no retry,
at most 20) and adds only those that ANSWER — i.e. hosts that have you on a list — then saves the
document (only after a relay answered the read, as always).

---

## 4. The console

1. The client sends `console.ticket {vm}` over Nostr (VM must be running; 6 tickets/minute per npub).
2. The host sets a fresh 8-character VNC password on the guest (QMP `set_password` + `expire_password`,
   60 s), READS both QMP replies, and only then returns `{ws, ticket, vnc_password, exp}` inside the
   NIP-44 result. A refused or unreadable reply refuses the ticket — there is no console without
   authentication. (`virsh qemu-monitor-command` exits 0 for an error reply, and HMP prints its errors
   as plain text, so the exit status alone says nothing.)
3. The client opens `wss://<host>/ws/vmconsole` and sends `{"t":"open","ticket":…}` as the **first
   frame** — never a query string (a ticket in the URL is refused: URLs land in proxy logs).
4. The host consumes the ticket (single use, 60 s, bound to that VM and npub), re-checks access and
   that the VM is running, connects to the guest's VNC on **127.0.0.1**, answers `{"t":"ok"}`.
5. The client attaches noVNC to the socket, then sends `{"t":"go"}`; raw RFB flows both ways.

Refusals are `{"t":"err","m":…}` messages, not HTTP statuses. Sessions end at
`vmhost_console_max_minutes`, or when the VM is stopped/deleted or the user unassigned. A live console
also RE-CHECKS its access (role, assignment, VM running, loopback display) right after it is
registered and every 30 s, so access removed behind the service's back — a domain edited with virsh,
a guest powered off from inside — closes it too; and stopping the host service (which a settings Save
does, to restart it with the new configuration) closes every console the old service had open.

Guests' VNC displays listen on **127.0.0.1 only** (the generated domain XML); a display found on any
other address is refused. The address is read from the domain's own `<graphics><listen address>` in
`virsh dumpxml`, not from `virsh vncdisplay`, which prints a bare `:0` for a display on `0.0.0.0`/`::`
— every address — that is easy to mistake for loopback.

**The VNC password.** Every generated domain carries `passwd="<random>"` with `passwdValidTo` in 1970,
so QEMU starts the display WITH password authentication and nobody holds a valid password until a
ticket rotates it. A display defined WITHOUT `passwd` has no password auth at all, and QEMU refuses
`set_password` for it. **Such VMs are refused a console, not migrated**: that covers any VM created by
phase 1 before this was fixed, and any VM defined by hand. To give one a console, shut it down, add a
password to its display (`virsh edit <vm>` →
`<graphics type='vnc' … passwd='xxxxxxxx' passwdValidTo='1970-01-01T00:00:01'>`) and start it again
(a password auth mode is only chosen when QEMU starts). While an unexpired ticket for a VM exists,
further tickets reuse its password (and extend its expiry), so two people sharing a VM do not lock
each other out; any revocation on the VM (unassign, stop, delete) forces the next ticket to rotate it. noVNC 1.5.0 is vendored in `static/vendor/novnc/` (see its README).

**"This computer"** (desktop app only): `window.pcVM` (desktop/vm.js, `qemu:///session`) is shown as the
FIRST host through `LocalHost` in vms.js, with the same client interface — list/get/power (start also
opens the display)/create (ISO from the file picker)/settings (`update`, `addDisk`, `addNetwork`,
`changeIso`, `ejectIso`, `gamingMouse` — sequential, first failure stops and is reported)/"Use installed
system" (`bootDisk`)/delete — and features `{assign:false, migrate:false, snapshots:false, console:"spice"}`:
the console button runs `pcVM.view` (SPICE), there is no ISO library, no assignment, no snapshots. It is
never written into `pcai:vmhosts`. The PosterChanOS "Local VMs" window (`os.js paintVmManager`) is KEPT:
it is a real compositor toplevel with its own window routing and six test files pinned to it, and
retiring it needs an on-device run this box cannot do (no X/Electron here).

Known trade-off: the VNC password is passed to `virsh qemu-monitor-command` as an argument, so another
local user on the host could see it in the process list for a moment. It expires in 60 seconds and
the display is loopback-only.

---

## 6. Cold migration (phase 3)

Move a VM from one PosterChan VM host to another. **Offline**: the VM is shut down first. The disks
are copied over HTTPS, the VM is defined on the target with its assignments, owner, labels, snapshots
and autostart, and the source keeps its copy for a while.

### Requirements

* **Admin on BOTH hosts.** The client signs an unpublished request, `vm.migrate.authorize {source,
  target, vm}`, NIP-44-encrypted to the **target**. The source carries it inside
  `peer.migrate.precheck`; the target decrypts it and checks the signer against ITS OWN admin list.
  The source can neither forge it nor read it. It is valid for 10 minutes and single-use.
* **Paired hosts.** Each host lists the other in `vmhost_peer_hosts`, one per line:
  `npub relay https` — e.g. `npub19q5… wss://nas.example/relay https://nas.example`. A line that
  does not parse is reported, never guessed at. Host↔host messages are the same kinds 5310/6310,
  signed by the node keys; a peer key can call ONLY `peer.migrate.*`, and an admin cannot call those.
* The source's HTTPS must reach its app at `/api/vmhost/transfer/` (see nginx above) and its
  `vmhost_public_url` must match the `https` the target lists for it.

### What v1 refuses (each with a message naming it)

* **VMs with a TPM** — every Windows VM this app creates has one. swtpm state is root-owned and lives
  outside the VM's directory, so it would not travel.
* host-device passthrough, shared host filesystems;
* disks that are not files inside the VM's own directory, qcow2 disks with a backing file, and
  external snapshots.
* A cdrom ISO that the target's library does not have (by file name) is **detached**, not refused.

### The state machine

Every transition is journaled (`<storage>/.state/migrations/<id>.json`, tmp → fsync → rename) BEFORE
the side effect it announces.

| Step | Source | Target |
|---|---|---|
| PLAN | `vm.migrate`: requester is admin here; target is a peer; VM is ours, not migrating, no TPM | `peer.migrate.precheck`: authorization valid and signer is admin HERE; free disk ≥ 1.1× the files + `vmhost_reserve_disk_gib`; no uuid/name collision; libvirt up; firmware present (when not auto-selected); storage writable → `prechecked` |
| QUIESCE | `quiescing`: `pc:migration state=outgoing` on the VM (start refused), autostart off, consoles closed, ACPI shutdown for `vmhost_shutdown_timeout_sec`; if it does not stop: abort, or `virsh destroy` when **force_shutdown** | |
| EXPORT | `exporting`: inactive `--migratable` XML, `pc:vm` metadata, each snapshot's XML, files + sha256 (in a thread); the manifest is written and its sha256 signed by the source key | |
| TRANSFER | `transferring`: `peer.migrate.begin {manifest_sha256, manifest_sig}` | `receiving`: fetches the manifest (checks hash, signature, ids, file names), pulls every file with `Range` into `.incoming/<id>/<name>.part`, resuming from the `.part` size |
| VERIFY + DEFINE | | `defining`: re-hash each file (mismatch → abort), move into `<storage>/<uuid>/`, rewrite disk/nvram paths **by file name only**, define with `pc:migration state=incoming` (start refused), redefine snapshots parents-first → `defined`, then `peer.migrate.commit` |
| HANDOFF | `handed_off` journaled FIRST, then undefine (`--snapshots-metadata --keep-nvram`) and move the directory to `.retained/<uuid>-<ts>/` → answer | on the answer: `committed`, clear the marker, restore autostart, start if **start_after** → `done`, `peer.migrate.ack` |
| DONE | ack → `done`; `.retained` reaped `vmhost_migration_keep_source_hours` after the ACK, never before | |

**The commit point is the source journaling `handed_off`.** Before it the source owns the VM: any
failure (checksum mismatch, a refused or failed transfer, a cancel, a crash in planned/quiescing/
exporting) makes the target delete `.incoming` and anything it defined, and the source clears the
marker, restores autostart and restarts the VM if it had been running. After it the target owns it.

**Progress** (kind 7310) comes from BOTH hosts, `e`-tagged to the authorization event and encrypted to
the requester, so the client subscribes on both relays with `#e:[authorization id]`. The client also
polls `vm.migrate.status` on both, so a dropped socket never freezes the bar.

### Transfer route

`GET /api/vmhost/transfer/{migration}/{index|manifest}` with `Range: bytes=N-`. Served only for a
migration this host is the SOURCE of, only while `exporting`/`transferring`, only to its target:
`Authorization: Nostr <base64 kind-27235>` signed by the target's node key, verified with the repo's
`git_auth.verify_nip98` (signature, `method GET`, ±60 s) PLUS an exact match of the `u` tag's path
(no query) and, when `vmhost_public_url` is set, its scheme and host — `verify_nip98` alone only checks
that the path CONTAINS a needle, and `/…/1` is a prefix of `/…/10`. 404 unknown migration, 401 any
credential problem, 409 wrong state or a file that changed since export, 416 bad range. No DB session;
1 MiB reads in a worker thread; `vmhost_transfer_max_mbps` caps both the serving and the pulling side.

**Event-loop cost, measured** (`tests/test_vmhost_transfer_lag.py`, real uvicorn on loopback, the
target pulling from another thread, a 5 ms sleep probe on the server loop): 1 GiB served at ~930 MiB/s
(page cache warm), probe lag median 0.38 ms, p99 3.97 ms, max 5.24 ms (idle max 2.0 ms). A real
network is slower, so the per-chunk hop costs less, not more. Not measured: a cold, slow disk (each
read blocks a worker thread, never the loop).

### Failure handling and recovery

* **Startup** resumes every unfinished journal: source planned/quiescing/exporting → abort and restore;
  transferring → keep serving and watching; handed_off → finish the undefine/retain idempotently, then
  ask the target. Target receiving → resume the pull from the `.part` sizes; defining → re-place and
  re-define idempotently; defined/locked → keep asking the source to commit; committed → finish.
* **The hosts lose each other at the commit point** (the source handed off, its answer never arrived):
  the target cannot know whether the handoff happened, and the source cannot know whether the target
  finished. After a contact deadline (10 min) both go **`locked`**: the pending VM cannot be started,
  the retained copy is not reaped, cancel is refused. Contact coming back resolves it automatically.
  Otherwise an admin runs **`vm.migrate.force_reclaim {migration, side, confirm:"split-brain"}`** on
  each host, where `side` names the host that KEEPS the VM: on the source `source` redefines it from
  `.retained` (`reclaimed`) and `target` releases it (`released`, the retained copy is kept until an
  admin deletes it — a forced decision is not an ack); on the target `target` finalises it (`done`)
  and `source` undefines and deletes its copy (`released`). The client asks for a confirm AND the
  VM's name, sends the same decision to both hosts, and says which ones answered. **Deciding without
  the other host can split-brain the VM** — two copies with the same identity and diverging disks —
  so make the same choice on both.
* **The target answers that it does not hold the VM** (`aborted`/`unknown`/`released`) after a
  handoff: the source takes it back automatically — an answer, unlike silence, cannot split it.
* **Cancel** (`vm.migrate.cancel`, source only) works until the handoff.

### Ops

Client (admin): `vm.migrate.precheck {vm, target, authz, start_after}` (dry run on both hosts),
`vm.migrate {vm, target, authz, start_after, force_shutdown}` → `{migration, precheck}` (returns after
the precheck; the rest runs in the background), `vm.migrate.status {migration?|vm?}` →
`{migrations, peers}`, `vm.migrate.cancel {migration}`, `vm.migrate.force_reclaim {migration, side,
confirm}`. Peer: `peer.migrate.precheck/begin/status/commit/ack/abort`. New error codes: `migrating`
(start/delete/assign refused while a migration holds the VM), `aborted` (the other host abandoned it).

### Not in v1

TPM VMs, live migration, sparse-file preservation (qcow2 is copied as allocated bytes), external
snapshots and backing chains, migrating between hosts that cannot reach each other's HTTPS, and a
bundled progress UI outside the Virtual Machines screen. VirshBackend's migration primitives
(`dumpxml --inactive --migratable`, `snapshot-list --topological`, `snapshot-dumpxml`,
`snapshot-create --redefine [--current]`, `undefine --snapshots-metadata`) are argv-only and have not
yet run against a real libvirt.

## 5. Code map and tests

| Piece | Where |
|---|---|
| wire kinds + relay rules | `app/services/vmhost/kinds.py` (used by `nostr_relay/server.py` + `thread.py`) |
| settings | `app/services/vmhost/config.py` (`DEFAULTS`, `DURABLE_KEYS`), `app/schemas.py`, `templates/admin/tabs/vmhost.html` |
| roles, ops, locks, capacity | `app/services/vmhost/service.py` |
| libvirt | `app/services/vmhost/backend.py` (`VirshBackend`: argv only, timeouts) |
| domain XML + `pc:vm` metadata | `app/services/vmhost/domainxml.py` |
| paths | `app/services/vmhost/storage.py` |
| replay/idempotency | `app/services/vmhost/journal.py` |
| Nostr transport, announcement, lifecycle | `app/services/vmhost/transport.py` |
| console tickets | `app/services/vmhost/console.py`; route in `app/routers/vmhost.py` |
| client | `static/js/client/vms.js` (screens), `vmrpc.js` (RPC), `vmconsole.js` (noVNC) |
| cold migration | `app/services/vmhost/migrate.py` (state machine, journal, peer RPC, transfer serving/pulling); ops registered at the bottom of `service.py`; primitives at the bottom of `backend.py`; route at the bottom of `app/routers/vmhost.py` |

Tests: `tests/test_vmhost_service.py`, `test_vmhost_transport.py`, `test_vmhost_relay_kinds.py`,
`test_vmhost_console.py`, `test_vmhost_admin_settings.py`, `test_vmhost_domainxml.py`,
`tests/client/test_vms_rpc.py` (+ `vms_rpc_runtime.mjs`, including a JS↔Python interop round trip),
`tests/client/test_vms_cache_first.py`, and `tests/client/test_vms_full_app.py` (real client in
Chrome, phone and desktop widths, real noVNC handshake drawing pixels; phase 2 adds the settings save,
a snapshot, the ISO library, Add host and Find hosts to the same run).

Phase 2: `tests/test_vmhost_phase2.py` (update incl. read-back and media replace, capacity, admin-only
table, snapshots + migrating, ISO fetch through the real SSRF guard incl. a redirect to 169.254.169.254
and header/stream caps, upload tickets through the real route, access durability, session keys through
the real transport with real signatures), `tests/client/test_vms_phase2.py` +
`vms_phase2_runtime.mjs` (LocalHost against a `pcVM` stub built from desktop/preload.js, sessions,
Find hosts), and the session-signer scenario in `vms_rpc_runtime.mjs`. Each rule was verified to fail
with its code mutated (25 mutations).

Migration: `tests/test_vmhost_migration.py` (two hosts with `tests/vmhost_migration_fake.py`, a fake
relay carrying real signed/encrypted 5310/6310/7310, the real transfer route over ASGI: happy path,
checksum mismatch, cut + Range resume, source crash after the commit point, lost contact → locked →
force reclaim both ways, route auth refusals, non-admin on the target, TPM, start refused while
outgoing/pending across a restart, shutdown timeout, peer parsing, nginx), `tests/test_vmhost_transfer_lag.py`
(event-loop lag), `tests/client/test_vms_migrate_full_app.py` (Migrate flow in Chrome at 390/1280).

Where the phases meet: `tests/test_vmhost_relay_kinds.py` — a peer host's 6310/7310/31310 pass the relay
gate (write path AND firehose) through the SHIPPED `_read_config` peer hook while a non-peer stranger's do
not, and not at all while hosting is off; `tests/test_vmhost_integration.py` — `vm.migrate.*` and
`peer.migrate.*` refuse a session key; a session key spends its owner's bucket; a peer host keeps its own
bucket and busy slots under a user flood; `vm.update` and a migration's rewritten definition both come
out with a VNC `passwd`; update/snapshots refuse a VM the migration journal holds.

| Piece (phase 2) | Where |
|---|---|
| hardware edits (XML tree) | `domainxml.py` (`read_hardware`, `set_*`, `add_*`, `secure_vnc`) |
| vm.update + snapshots | `app/services/vmhost/hardware.py` |
| ISO fetch/upload/delete | `app/services/vmhost/isolib.py`; PUT route in `app/routers/vmhost.py` |
| host.access | `app/services/vmhost/access.py` |
| session keys | `app/services/vmhost/sessions.py`; resolution in `transport.py` |
