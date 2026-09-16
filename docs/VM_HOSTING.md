# VM hosting (Virtual Machines)

PosterChan's own Proxmox-like VM hosting. A PosterChan **server node** with libvirt runs virtual
machines; admins create, delete and assign them; the people they are assigned to start, stop, reboot
and open their console — from the **Virtual Machines** screen of any PosterChan client (web, Android,
desktop, even a bundle with only a Nostr key and relays).

Management is **encrypted Nostr**, not an HTTP API: every operation is a signed kind-5310 event,
NIP-44-encrypted to the host node's key. The only thing that uses the node's HTTPS is the console
(noVNC), and it needs a one-use ticket that was itself issued over Nostr.

**Phase 1**: host info, VM list/get, power, create from a library ISO, delete, assign/unassign,
console. **Phase 3**: cold migration between hosts (§6). Not yet: editing hardware, snapshots
management, ISO download/upload, session keys, host discovery (phase 2).

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
/var/lib/posterchan/vms/.state/                    lock + op journal
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
| anyone else | — | nothing. Their requests are **dropped silently** — no reply at all |

A user asking about a VM that is not theirs gets `not_found`, never `forbidden` (which would confirm it
exists). Unassigning somebody closes their open console immediately.

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

Error codes: `bad_request forbidden not_found conflict busy insufficient_capacity unsupported
rate_limited backend_error timeout version internal`.

**Client correlation:** sign the request, SUBSCRIBE to `{kinds:[6310,7310], authors:[host], #e:[id]}`,
THEN publish. A result counts only if it verifies, is authored by the host, e-tags the request and
carries the same `id`.

**Replay and retries.** The host drops an event older than 120 s, more than 30 s in the future,
without an expiration, expired, or already seen. A client that heard nothing retries with a NEW event
carrying the SAME `id`; for mutating ops the host's journal (`.state/journal/ops.jsonl`, 15 min)
returns the stored result instead of powering the VM twice. Only successes are journaled — a refusal
(busy, capacity) lets the retry actually retry.

**Relay carriage.** The requester is usually outside the relay's web of trust. The relay's write gate
and firehose (`app/services/vmhost/kinds.py`) accept a non-member's 5310 only when it is addressed to
THIS node, short-lived, `nofederate` and ≤ 70 KB; results are accepted for the node's own users.
Turning the host on adds a firehose subscription for 5310/6310/7310 `#p`-tagged to the node (applied
live via reload-upstream).

**No answer is not "no VMs".** An offline host and a host that drops strangers look identical to a
client; the UI says "No answer — the host is offline or you're not on its list" and keeps the last
known list on screen.

---

## 4. The console

1. The client sends `console.ticket {vm}` over Nostr (VM must be running; 6 tickets/minute per npub).
2. The host sets a fresh 8-character VNC password on the guest (QMP `set_password` + `expire_password`,
   60 s) and returns `{ws, ticket, vnc_password, exp}` inside the NIP-44 result.
3. The client opens `wss://<host>/ws/vmconsole` and sends `{"t":"open","ticket":…}` as the **first
   frame** — never a query string (a ticket in the URL is refused: URLs land in proxy logs).
4. The host consumes the ticket (single use, 60 s, bound to that VM and npub), re-checks access and
   that the VM is running, connects to the guest's VNC on **127.0.0.1**, answers `{"t":"ok"}`.
5. The client attaches noVNC to the socket, then sends `{"t":"go"}`; raw RFB flows both ways.

Refusals are `{"t":"err","m":…}` messages, not HTTP statuses. Sessions end at
`vmhost_console_max_minutes`, or when the VM is stopped/deleted or the user unassigned.

Guests' VNC displays listen on **127.0.0.1 only** (the generated domain XML); a display found on any
other address is refused. noVNC 1.5.0 is vendored in `static/vendor/novnc/` (see its README).

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
Chrome, phone and desktop widths, real noVNC handshake drawing pixels).
Migration: `tests/test_vmhost_migration.py` (two hosts with `tests/vmhost_migration_fake.py`, a fake
relay carrying real signed/encrypted 5310/6310/7310, the real transfer route over ASGI: happy path,
checksum mismatch, cut + Range resume, source crash after the commit point, lost contact → locked →
force reclaim both ways, route auth refusals, non-admin on the target, TPM, start refused while
outgoing/pending across a restart, shutdown timeout, peer parsing, nginx), `tests/test_vmhost_transfer_lag.py`
(event-loop lag), `tests/client/test_vms_migrate_full_app.py` (Migrate flow in Chrome at 390/1280).
