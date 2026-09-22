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
the desktop app. **Phase 3**: cold migration between hosts (§6). **Phase 4**: the first live run against
real libvirt (§7, `scripts/vmhost_live_probe.py`), offline snapshots, `install.sh --vmhost`, the desktop
shortcut opening this screen, per-op audit logging.

---

## 1. Set up a host

### One command

```sh
./install.sh --vmhost          # as the user posterchanai.service runs as; uses sudo for the system changes
```

`scripts/install/vmhost.sh`, idempotent (a re-run only repairs what drifted — tested by running it twice under
stubbed system commands, `tests/test_install_vmhost.py`, and dry-run on the live host with every privileged
change refused). It does, in order:

1. **Packages**, skipped when `virsh` and `qemu-img` already exist: Gentoo `emerge --noreplace
   app-emulation/libvirt app-emulation/qemu` (NOT `sys-firmware/edk2-bin`: qemu pins its own edk2 and an
   explicit request conflicts), Debian/Ubuntu `libvirt-daemon-system libvirt-clients qemu-system-x86
   qemu-utils ovmf`, Arch `libvirt qemu-base edk2-ovmf dnsmasq`, Fedora `libvirt-daemon-kvm libvirt-client
   qemu-kvm qemu-img edk2-ovmf`, openSUSE `libvirt qemu-kvm qemu-tools qemu-ovmf-x86_64`.
2. The service user joins **`libvirt`** and **`kvm`** (effective when the service restarts).
3. **Storage** `/var/lib/posterchan/vms` (`VMHOST_STORAGE` to change it): owned `<user>:qemu`
   (Debian `<user>:libvirt-qemu`), **0751**, `isos/` **0755**. On **btrfs**, `chattr +C` (NOCOW) on it,
   on `isos/` and on `/var/lib/libvirt/images` — ONLY while each is still empty, because NOCOW applies to
   files created afterwards; a populated directory gets a warning instead (move the images out, `chattr +C`,
   copy them back with `cp`). Warns about a parent directory QEMU cannot traverse.
4. **libvirtd** enabled. When libvirt has **no polkit** (no `org.libvirt.unix.policy`; Gentoo's default), its
   read-write socket is `root:root 0600` and every group membership is useless, so it writes
   `/etc/systemd/system/libvirtd.socket.d/posterchan-group.conf` (`SocketGroup=libvirt`, `SocketMode=0660`),
   sets `unix_sock_group = "libvirt"` and `unix_sock_rw_perms = "0770"` in `/etc/libvirt/libvirtd.conf`, and
   restarts the socket — only when something changed. With polkit nothing is touched.
5. The **`default`** NAT network defined (if missing), autostarted and started.
6. `/dev/kvm` checked (without it VMs are slow software emulation).
7. **Final check**: `virsh -c qemu:///system list --all` as the service user.

> **Security: membership of `libvirt` is effectively root on this machine.** Anyone who can drive
> `qemu:///system` can define a VM that mounts `/` from the host. Enabling VM hosting means trusting
> this app — and therefore every admin npub you list — with that. Keep the admin list short.

Then restart `posterchanai.service` (group changes need a new process) and turn it on in Admin → VMs.

### Storage

Layout, all built by the server from ids — **clients never send a path**:

```
/var/lib/posterchan/vms/<vm-uuid>/disk-vda.qcow2   the disk (0600)
/var/lib/posterchan/vms/<vm-uuid>/nvram.fd         EFI variables (0600, created by the APP from libvirt's template)
/var/lib/posterchan/vms/<vm-uuid>/snap-<name>.nvram.fd   the EFI variables an offline snapshot saved
/var/lib/posterchan/vms/<vm-uuid>/domain.xml       the definition handed to libvirt (recovery copy)
/var/lib/posterchan/vms/isos/*.iso                 the ISO library (copy installers here)
/var/lib/posterchan/vms/<vm-uuid>/disk-vdb.qcow2   a disk added later in VM Settings (vdb…vdz; sdb… on Windows)
/var/lib/posterchan/vms/isos/.incoming/*.part      downloads/uploads until they are whole (never listed)
/var/lib/posterchan/vms/.state/                    lock + op journal + sessions.json (0600)
```

An ISO id is a bare file name, and its RESOLVED path must still be a regular file inside `isos/` —
a symlink pointing out of the library is refused, not followed.

**Two Unix users touch these files, and that shaped the modes** (both found on the first live run, §7).
libvirt runs QEMU as `qemu` (Debian: `libvirt-qemu`) and, with its default `dynamic_ownership`, chowns a
DISK to that user while the guest runs and gives it back on stop — but never the directories QEMU walks
through. So the storage root and every VM directory are **0751** (traversable, not listable), the ISO
library **0755**, disks and variable stores **0600**, `.state` 0700. A VM directory created 0750 made every
VM fail its first start with `Cannot access storage file … (as uid:77, gid:77): Permission denied`.

**EFI variable store.** libvirt ≥ 8 writes the firmware template it auto-selected into the definition
(`<nvram template=… format=…>`) and, left alone, creates `nvram.fd` at the FIRST START — as `qemu:qemu 0600`,
for good: the app could never read it again, so no migration (hashing it raised PermissionError) and no
snapshot could include it. The host now copies that template itself right after defining the VM, before
any start; libvirt then chowns the app's file to qemu while the guest runs and restores it on every stop.
A VM created before this fix has a qemu-owned `nvram.fd`: `sudo chown <app user> <dir>/nvram.fd` once.

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

Any host that takes ISO uploads needs the upload location too (also shipped in both configs):

```nginx
location ^~ /api/vmhost/iso/ {
    proxy_pass http://posterchanai_app;
    proxy_request_buffering off;
    client_max_body_size 0;
    proxy_read_timeout 3600s;
}
```

Both must come before `location ^~ /api/`. Don't add a `proxy_set_header` to either — see
`docs/NGINX.md`. (`tests/test_vmhost_iso_limits.py` parses both shipped configs and checks these directives.)

### Audit log

Every answered request writes one line to the app log (`journalctl -u posterchanai.service | grep vmhost-audit`):

```
[vmhost-audit] op=vm.power by=<requester hex> role=user session=yes vm=<uuid> action=start result=ok
```

Who (the requester's pubkey; `session=yes` when a session key signed for it), what (the op and the ids it named:
`vm`, `action`, assignment `target`, `snapshot`, `migration`) and the result (`ok` or the error code — refusals
are logged too). It is built from an allowlist of id-shaped fields, never from the arguments or the result: a
console ticket, a VNC password, an ISO URL (which can carry credentials) or a description never reaches the log.
Changes, console tickets, sessions, upload tickets and every refusal log at INFO; successful plain reads at
DEBUG. Strangers are dropped before this, with no line at all.

The status panel (**Check VM host status**) also says whether the storage directory is traversable by the
qemu user. The `vmhost_backend` setting ("auto (virsh)" / "virsh", which did the same thing) was removed.

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

Everything that changes a VM or the host — create, delete, assign, **settings, snapshots, devices, ISO
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

The announcement is published when the host starts — so every Save (which restarts it) republishes it with the
new name/URL/relay at once — and every 6 h. Its `features` are the SAME list `host.whoami` answers. Turning
**Publish a public host announcement** off publishes a NIP-09 kind-5 deletion (`a` = `31310:<node>:posterchan-vmhost`)
on every start, and switching hosting off retracts an announcement this process published; an addressable
event otherwise stays on every relay that took it.

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
| `vm.snapshot.list/create/delete` | `vm`, `name` (≤48, `[A-Za-z0-9_.-]`), `description?` | **offline**, ≤32 per VM (§5a). create/delete need the VM **shut off** (`conflict` otherwise); list works any time and returns each snapshot's `state` checked against the disks: `ok`, `disks_changed`, `incomplete`, or `orphan` (a tag no record names — a create that died half-way; delete removes it). Refused: a raw disk, a variable store the app cannot read (`unsupported`), < 1 GiB free above the reserve (`insufficient_capacity`) |
| `vm.snapshot.revert` | `vm`, `name`, `confirm: true` | shut off; without `confirm` → `bad_request`; only an `ok` snapshot; closes consoles. Restores the disks and the EFI variables only — the definition (hardware, `pc:vm` access, a migration tag) stays CURRENT |
| `iso.list` | — | now also `jobs` (downloads, with state) and `fetch_enabled` |
| `iso.fetch` | `url`, `name?` | answers AT ONCE with `{job}` — the download is a background job; 7310 progress (tagged to this request) every 2 s and a final `done`/`failed` |
| `iso.fetch.status` | `job?` | `{job}` or `{jobs}`: `state` running/done/failed/cancelled, `bytes`, `total`, `iso`, `error`; kept an hour |
| `iso.fetch.cancel` | `job` | stops the download and removes its `.part` |
| `iso.upload_ticket` | `name`, `size` (bytes) | → `{ticket, url, name, size, exp}`; 10 min, single use |
| `iso.delete` | `iso` | `conflict` while any VM's metadata or cdrom still uses it — checked under the HOST lock, which every attach holds |
| `host.access.get/set` | `set`: `allowed: [npub or hex]` | see §2 |
| `session.open` | `pk`, `exp`, `scope: "use"`, `proof` | user; must be the REAL key |
| `session.close` | `pk?` | a session closes itself; a real key closes one or all of its own |
| `host.devices.list` | `kind?`, `vm?` | admin. Per kind (`usb`, `pci`): the host checks, and the devices it can give — each with `busy`, `used_by` and, for PCI, its checks, IOMMU group and the functions that go with it (§5b) |
| `vm.device.attach` | `vm`, `kind`; usb: `vendor`, `product`, `bus?`, `device?`, `persist?`; pci: `address` | admin. USB into a RUNNING VM (`--live`, plus `--config` when `persist`) or a stopped one (`--config`); PCI only while shut off. Read back from the live and saved definitions |
| `vm.device.detach` | as attach | admin; read back. `vm.get` answers `devices` to every role that can see the VM (read only) |

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

Downloading a URL on behalf of a client is server-side request forgery unless proven otherwise.

* **Syntax** first (`rss_service.looks_fetchable`: http/https only, no localhost / `.lan` / `.local`, no
  private IP literal).
* **Resolve ONCE, connect to what was checked** (`isolib.PinnedTransport`). Every address the name resolves
  to must be public (`isolib.ip_blocked`: private, loopback, link-local, multicast, reserved, unspecified,
  non-global, plus `0.0.0.0/8`, `100.64.0.0/10`, `192.0.0.0/24`, `198.18.0.0/15`, `240.0.0.0/4`,
  `64:ff9b::/96`, `64:ff9b:1::/48`, and the IPv4-mapped/-compatible, 6to4 and Teredo forms of any blocked
  IPv4). The connection goes to that address with the `Host` header and TLS SNI — so certificate
  verification — still on the name. Checking a name and letting the HTTP client resolve it again is DNS
  rebinding: public on the first answer, `127.0.0.1` on the second.
* **Redirects by hand**, each hop resolved, checked and pinned again — a public URL that 302s to
  `169.254.169.254` is refused and never requested (`search_service.fetch_url_content` once got exactly
  this wrong).
* **No proxy**: the client is built with `trust_env=False`, so `HTTP(S)_PROXY` in the service environment
  is ignored; never the Tor fallback transport either.
* Bytes stream to `isos/.incoming/<random>.part` with a running SHA-256 and a hard cap of
  `min(32 GiB, room)`, enforced on the `Content-Length` before reading AND on the stream. Only a whole file
  is linked into the library, never over an existing name. `vmhost_iso_fetch_enabled` turns it off.

**Limits.** At most **2** ISO transfers (fetches and uploads together) run on a host (`busy` beyond). The
room a new transfer may use is the free disk minus `vmhost_reserve_disk_gib` minus what is already
PROMISED: every running transfer's size (a fetch reserves its cap until `Content-Length` is known), every
incoming migration's outstanding bytes, and the unallocated part of every thin VM disk (provisioned
`disk_gib` minus the blocks its directory actually uses). The fetch runs as a background job, so it holds
no admin busy slot; `transport.stop()` cancels running downloads. Stale `.part` files and migration
`.incoming/<id>` directories nothing owns are removed at startup and hourly.

### ISO upload

`iso.upload_ticket` (admin, over Nostr) → `PUT <https>/api/vmhost/iso/<ticket>` with the raw bytes.
The ticket is consumed BEFORE the body is read (so a copy in a proxy log is already spent), re-checks
that its npub is STILL an admin, and the body is streamed to a `.part` and cut off the moment it passes
the declared size; a body shorter or longer than declared is discarded. The route answers CORS `*`
(`/api/vmhost/iso/` is in `_ScopedCORS._OWN_CORS`) because the client usually lives on another origin
and the credential is the ticket, never a cookie. An upload counts against the 2-transfer cap and reserves
its declared size for as long as it runs. nginx: `location ^~ /api/vmhost/iso/` streams the body
(`proxy_request_buffering off`, `client_max_body_size 0`, 3600 s) — see §1.

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
"host offline" — from a bounded in-memory tombstone set (4096, 16 per owner); the session itself leaves
`.state/sessions.json` at once. Only LIVE sessions are persisted: ≤8 per owner (the oldest is closed),
≤4096 per host (`busy` beyond), written off the event loop (tmp + fsync + rename, coalesced). A local-nsec
client never opens one. Client side (`vms.js`): the session SECRET is kept **in memory only** — never in
localStorage, where any script on the origin reads it and it outlives a sign-out — and dropped whenever
the signed-in account changes (a reload costs one `session.open`); secrets an older build stored are
purged when the screen loads. Polling ops use the session; everything else, and `session.open`
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

### Networks and bridges

A VM is on the host's default wire (`vmhost_default_network`, or `vmhost_bridge` when set) unless the
admin chooses one: `vm.create` and `vm.update` take `network: {"type": "network"|"bridge", "name": …}`.
The name must be one the host has **right now** — `host.info` (admin) lists them as `networks` (libvirt,
`virsh net-list --all --name`) and `bridges` (Linux bridges, `/sys/class/net/*/bridge`), plus
`default_network` and `bridge`. Anything else — an unknown name, a name shaped like markup, another
interface type — is `bad_request` before a byte is written; a client string never reaches the XML
unchecked. `vm.update` moves the FIRST adapter (keeping its MAC and model; a VM with none gets one), is
confirmed by reading `hardware.net` back, and an `add_nic` in the same Save goes on the chosen wire.

Views carry `net: {type, name}` (the primary adapter, from `virsh domiflist --inactive`). A RUNNING VM also
carries `ips` (`virsh domifaddr --source lease`, falling back to `--source arp` for a bridged guest whose
lease libvirt never sees; cached per VM for 60 s so the client's poll is not a virsh call per VM per poll)
and `uptime_s` (the qemu process's start time from `/proc`, matched by its `-uuid`). All three are
best-effort: a failed read leaves the field absent (or `ips: []`), never fails the op.

On a PosterChanOS desktop the bridge itself is made in **System Settings → Network → Bridges for virtual
machines** (`desktop/net.js`, through `sudo -n nmcli`, rolled back if the bridge gets no address), which
also allows it in `/etc/qemu/bridge.conf` and makes `qemu-bridge-helper` setuid so this computer's
`qemu:///session` VMs can join it (`pcVM.create({network})` / `pcVM.setNetwork`).

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
never written into `pcai:vmhosts`. **Phase 4 retired the desktop's separate "Local VMs" window** (`os.js
paintVmManager`, view `__vms`): the start menu and the desktop icon open this screen, once, with "This
computer" first. `__vms` survives only as an alias (`os.js LEGACY_VIEWS`, `oswin.js`, `app.js switchView`)
so an old taskbar pin or a monitor handoff from an older shell lands on `vms`
(`tests/client/test_vms_desktop_shortcut_full_app.py`, red against the previous bundle).

Known trade-off: the VNC password is passed to `virsh qemu-monitor-command` as an argument, so another
local user on the host could see it in the process list for a moment. It expires in 60 seconds and
the display is loopback-only.

---

## 6. Cold migration (phase 3)

Move a VM from one PosterChan VM host to another. **Offline**: the VM is shut down first. The disks
are copied over HTTPS, the VM is defined on the target with its assignments, owner, labels, snapshots
and autostart, and the source keeps its copy for a while.

### Security model: each host treats the other as hostile

Pairing two hosts in `vmhost_peer_hosts` lets them run a migration together; it does NOT make either an
administrator of the other. A domain definition is root on the host that defines it, and a disk image can
name files on it; the copy the source hands over is the only copy left once the source lets go. So:

**The target never defines the source's XML — it REBUILDS the domain.** From the source's definition it
takes only validated values: name and uuid (which must equal the precheck's, or the migration is refused —
a definition wearing another VM's uuid would redefine that VM), vCPUs and memory (within this host's
limits), `efi|bios`, the chipset family (`q35` or `pc`; x86_64 only), file disks by their MANIFEST name
and target (`vdX`/`sdX`), unicast MACs and a NIC model from an allowlist, tablet/mouse. It builds the
definition with this host's own generator (`domainxml.build_domain_xml`): this host's network or bridge,
ONE VNC display on 127.0.0.1 with an expired password, a virtio video, an EMPTY cdrom, this host's
emulator and firmware, and `pc:vm` metadata rebuilt from validated fields. Everything else is dropped —
`qemu:commandline` under any prefix, `<seclabel>`, serial/console/channel/parallel devices on host paths,
`<kernel>`/`<initrd>`/`<dtb>`/`<cmdline>`, nvram templates, ethernet/direct interfaces and their scripts,
SPICE and non-loopback or socket displays, custom emulators.

**Refused, not dropped** (a silent drop would change what the machine is): a TPM, host devices, shared
filesystems, any disk that is not a plain file (block, volume, network — or a file disk that also names a
`dev`), backing chains and external overlays, a disk the transfer did not carry, a foreign architecture,
external snapshots (disk or memory), and snapshots with invalid, duplicate or out-of-order names. The
target runs the source's own refusals (`inspect_domain_xml` + `_refusals`) as well, and validates the
definition BEFORE pulling any bytes.

**Snapshots** are re-created from name, description, state, creationTime, parent and internal disk names
only, around the target's own rebuilt definition — never the `<domain>` the source embedded.

**Disks are probed.** After its checksum, every received disk is run through `qemu-img info
--output=json -U` (argv, timeout): a backing file, an external data file, or a format other than qcow2/raw
refuses the migration, and `<driver type>` is the PROBED format, not the source's claim.

**Assignments** come from the target admin's signature, not the source: the client signs the VM's current
assigned list inside `vm.migrate.authorize`, and the target carries only those pubkeys (none when the list
is absent).

**Resources.** The manifest is read with an 8 MiB cap; its total may not exceed what the precheck reserved
nor the free space minus the reserve and every other reservation (the precheck counts those too); a body is
cut at the declared size; retries are bounded in total, not only per stall.

**The source does not take the target's word.** It records the byte ranges it actually SERVED per file and
refuses a commit until every file was served whole; then it challenges the target (`peer.migrate.challenge`:
a fresh nonce and random ranges of every file) and compares SHA-256(nonce ‖ bytes) with its own copy — a
wrong answer aborts the migration and the VM stays. Once challenged, the transfer route is closed. The
handoff is complete only when libvirt confirms the domain is gone from the source; until then it is retried
on every commit, ack and watch tick, the source does not reach `done`, and start is refused on the source
for as long as the VM's latest migration record says it left (also after `done`). The retained copy is kept
`vmhost_migration_keep_source_hours` after the ack — **0 keeps it until an admin deletes it**.

**Transfer credentials** are single use (the event id is remembered for the freshness window; each header
carries a nonce), and `vmhost_transfer_max_mbps` holds for the whole migration, however many parallel range
requests the target makes.

### Requirements

* **Admin on BOTH hosts.** The client signs an unpublished request, `vm.migrate.authorize {source,
  target, vm, assigned}`, NIP-44-encrypted to the **target**. The source carries it inside
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
* disks that are not files inside the VM's own directory, qcow2 disks with a backing file or an external
  data file, and external snapshots.
* The target refuses the same things again itself, and more — see *Security model* above.
* The cdrom arrives **detached** (empty): installer media is the target library's business.

### The state machine

Every transition is journaled (`<storage>/.state/migrations/<id>.json`, tmp → fsync → rename) BEFORE
the side effect it announces.

| Step | Source | Target |
|---|---|---|
| PLAN | `vm.migrate`: requester is admin here; target is a peer; VM is ours, not migrating, no TPM | `peer.migrate.precheck`: authorization valid and signer is admin HERE (its assigned list kept); free disk ≥ 1.1× the files + `vmhost_reserve_disk_gib` + every other reservation; no uuid/name collision; libvirt up; storage writable (the source's firmware PATH is not checked: `--migratable` XML spells out the source distro's auto-selected loader, and the rebuilt definition asks this host's libvirt for `firmware="efi"` instead) → `prechecked` |
| QUIESCE | `quiescing`: `pc:migration state=outgoing` on the VM (start refused), autostart off, consoles closed, ACPI shutdown for `vmhost_shutdown_timeout_sec`; if it does not stop: abort, or `virsh destroy` when **force_shutdown** | |
| EXPORT | `exporting`: inactive `--migratable` XML, `pc:vm` metadata, each snapshot's XML, files + sha256 (in a thread); the manifest is written and its sha256 signed by the source key | |
| TRANSFER | `transferring`: `peer.migrate.begin {manifest_sha256, manifest_sig}`; records the ranges it serves | `receiving`: fetches the manifest (≤ 8 MiB; hash, signature, ids, name, file names `disk-(vd\|sd)X.qcow2`/`nvram.fd`/`snap-<name>.nvram.fd`, total ≤ the precheck's, the definition VALIDATED), pulls every file with `Range` into `.incoming/<id>/<name>.part`, resuming from the `.part` size, never writing past the declared size |
| VERIFY + DEFINE | | each file hashed (mismatch → abort) and each disk AND variable store PROBED (`qemu-img info`: a qcow2 varstore with a backing file is refused like a disk); `defining`: move into `<storage>/<uuid>/` (0751, files 0600), define the REBUILT domain with `pc:migration state=incoming` (start refused) and `<nvram format>` = the probed format (so this host's libvirt picks a firmware that can read it), keep only the offline-snapshot records the transferred bytes back (every disk holds the tag once, the variable-store copy arrived), re-create any libvirt snapshot metadata parents-first → `defined` |
| PROOF | `peer.migrate.challenge` (only once every byte was served): journals a nonce + ranges, closes the route | answers SHA-256(nonce ‖ bytes) of its placed copy, then `peer.migrate.commit {proofs}` |
| HANDOFF | proofs checked against its own files (wrong → abort); `handed_off` journaled FIRST, then undefine (`--snapshots-metadata --keep-nvram`), confirmed gone, and the directory moved to `.retained/<uuid>-<ts>/` → answer | on the answer: `committed`, clear the marker, restore autostart, start if **start_after** → `done`, `peer.migrate.ack` |
| DONE | ack (only once the undefine is confirmed) → `done`; `.retained` reaped `vmhost_migration_keep_source_hours` after the ACK, never before, never when 0 | |

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
that the path CONTAINS a needle, and `/…/1` is a prefix of `/…/10`. Each header is accepted ONCE. Files are
served only while `transferring` and not after the challenge. 404 unknown migration, 401 any credential
problem (including a reused header), 409 wrong state or a file that changed since export, 416 bad range.
No DB session; 1 MiB reads in a worker thread; `vmhost_transfer_max_mbps` caps the serving side per
migration (across parallel requests) and the pulling side.

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
* **The target answers that it does not hold the VM** (`aborted`/`released`) after a
  handoff: the source takes it back automatically — an answer, unlike silence, cannot split it. A target
  that answers `unknown` (its journal forgot the migration) is treated as silence, and a source that
  answers `not_found` never makes a target drop its copy: a defined target locks, a locked one waits for
  force_reclaim.
* **A failed undefine at the handoff** (libvirt busy, a lock) leaves `handed_off` with the VM still
  defined: the source retries it on every commit, ack and watch tick and cannot reach `done` before it
  succeeds; start stays refused there throughout.
* **Cancel** (`vm.migrate.cancel`, source only) works until the handoff. On a `released` migration (the
  target was force-kept) it answers with the split-brain warning and, with `confirm: "split-brain"`, takes
  the retained copy back (`reclaimed`).
* **Retained copies**: a `released` copy is reaped by the same keep rule as an acked one (counted from the
  forced decision). `vm.retained.list` shows every `.retained/` directory with its migration and state;
  `vm.retained.delete {name, confirm: "delete"}` removes one — refused while its migration is
  `handed_off` or `locked`, where it may be the only copy.
* **A source restart mid-transfer** loses up to 64 MiB of served-range bookkeeping per file (it is journaled
  in 64 MiB steps and at the end of every response); if that leaves a file short of "served whole", the
  target's commit is refused until an admin cancels and migrates again.

### Ops

Client (admin): `vm.migrate.precheck {vm, target, authz, start_after}` (dry run on both hosts),
`vm.migrate {vm, target, authz, start_after, force_shutdown}` → `{migration, precheck}` (returns after
the precheck; the rest runs in the background), `vm.migrate.status {migration?|vm?}` →
`{migrations, peers}`, `vm.migrate.cancel {migration}`, `vm.migrate.force_reclaim {migration, side,
confirm}`, `vm.retained.list`, `vm.retained.delete {name, confirm}`. Peer:
`peer.migrate.precheck/begin/status/challenge/commit/ack/abort`. New error codes: `migrating`
(start/delete/assign refused while a migration holds the VM), `aborted` (the other host abandoned it).

### Not in v1

TPM VMs, live migration, sparse-file preservation (qcow2 is copied as allocated bytes), external
snapshots and backing chains, migrating between hosts that cannot reach each other's HTTPS, and a
bundled progress UI outside the Virtual Machines screen. The migration primitives have run against real
libvirt 12 on one host (§7: export, `undefine --snapshots-metadata --keep-nvram`, and the target's rebuild
defined in a second storage root, booted, console authenticated, offline snapshots carried and reverted);
a real TWO-host migration still needs a second KVM-capable host.

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

Hardening (each written to fail on the code before its fix): `tests/test_vmhost_migration_hostile.py` (a
hostile source: every dropped construct absent from what the target defines, every dangerous one refused,
a definition wearing another VM's uuid/name refused with the victim untouched, an ordinary VM round-trips
vCPUs/RAM/disks/MAC/assignments/snapshots), `test_vmhost_migration_disk_probe.py` (qemu-img JSON shapes
through the runner seam; backing/data-file/vmdk refused; driver type = probe),
`test_vmhost_migration_lying_target.py` (commit without download, corrupt copy, guessed challenge, keep=0),
`test_vmhost_migration_handoff.py` (failed undefine retried, never startable on the source),
`test_vmhost_migration_target_resources.py` (manifest cap, precheck/reservation totals, truncation, bounded
retries), `test_vmhost_migration_low.py` (signed assignments, not_found while locked, single-use NIP-98,
per-migration throttle, retained copies, cancel after release), `test_vmhost_iso_fetch_pinning.py` (DNS
rebinding, env proxy, every blocked range — through the real httpx transport), `test_vmhost_iso_limits.py`
(background jobs, concurrency, reservations incl. thin disks, stale parts, iso.delete race, parsed nginx),
`test_vmhost_sessions_bounded.py` (10k opens: bounded file, no loop stall), `test_vmhost_reload.py` (old
handlers cancelled by stop, one migrator per journal), `test_vmhost_snapshots_offline.py` (offline
snapshots: round trip restores disk bytes and variables, shut-off rule, orphan tags, rollback half-way,
revert keeps CURRENT access and hardware, a disk added later, migration carries them and drops records the
bytes do not back, a hostile variable-store copy refused), `test_vmhost_backend_migration.py` (the REAL VirshBackend has the
migration primitives — a merge had left them unreachable — and their argv).

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

Live (phase 4): `scripts/vmhost_live_probe.py` (§7) + `scripts/vmhost_rfb.py` (RFB/VNC-auth client, DES
cross-checked against `cryptography`); `tests/test_vmhost_real_captures.py` runs every parser on what the
live host printed (`tests/fixtures/vmhost_real/`, with `index.json` holding argv/rc/stderr);
`tests/test_vmhost_live_findings.py` — one regression test per live bug, each shown failing first.

| Piece (phase 2) | Where |
|---|---|
| hardware edits (XML tree) | `domainxml.py` (`read_hardware`, `set_*`, `add_*`, `secure_vnc`) |
| vm.update + snapshots | `app/services/vmhost/hardware.py` |
| ISO fetch/upload/delete | `app/services/vmhost/isolib.py`; PUT route in `app/routers/vmhost.py` |
| host.access | `app/services/vmhost/access.py` |
| session keys | `app/services/vmhost/sessions.py`; resolution in `transport.py` |

## 5a. Offline snapshots

A snapshot is taken of a **shut-off** VM: `qemu-img snapshot -c <name> -- <disk>` on every qcow2 disk, a copy
of `nvram.fd` to `snap-<name>.nvram.fd` (0600, written atomically), and a record in the VM's `pc:vm`
metadata — `<pc:snapshot name created disks="vda,vdb" nvram="1">description</pc:snapshot>`. Revert is
`qemu-img snapshot -a` on the same disks and the copy put back; delete is `-d` (every copy of a repeated
tag) plus the copy removed and the record dropped. A create that fails half-way deletes the tags it made.

**Why not libvirt's internal snapshots.** Measured on the live host, whether libvirt accepts one for an EFI
guest depends on the distro's firmware descriptors: libvirt 12 with Gentoo's qcow2 varstore took an internal
snapshot of a RUNNING EFI VM, while a raw OVMF varstore (Debian's default) is refused. One button that works
on one host and fails on the next — and a migration that has to carry two formats — was the worse design.
The offline path behaves the same on every host, only files travel, and a revert never touches the
definition, so assignments, hardware and a migration tag stay current by construction (the service still
compares the metadata after a revert and re-applies the current copy if anything changed it).

What a snapshot does NOT capture: hardware settings (vCPUs, memory, NICs) and anything a running guest had
in memory. A snapshot taken before a disk was added cannot be reverted (`disks_changed`) — it can only be
deleted. qemu-img allows two snapshots with the same tag (measured: `-c s1` twice made IDs 1 and 3), so a
create refuses a name any disk already holds, and list shows such leftovers as `orphan`.

## 5b. Devices (USB and PCI passthrough)

A VM page has a **Devices** section; admins get **Add device** (a USB tab and a PCI tab) and **Detach**, an
assigned user sees the list only. Code: `app/services/vmhost/devices.py` (the ops, kind-generic — a new kind is one
class), `usb.py` and `pci.py` (read the host from sysfs; nothing there binds or loads anything), and for "This
computer" `desktop/vmusb.js`. Clients send ids only — `vendor`/`product` exactly `^[0-9a-f]{4}$`, `bus`/`device`
numbers, a PCI `address` like `0000:01:00.0`; the `<hostdev>` XML is built on the host, and the device must exist
on the host at that moment.

**Never offered:** hubs and root hubs, the device the host boots from, PCI bridges and host plumbing. "The device
the host boots from" is traced every way a mount can name its disk: the SOURCE path, the major:minor field through
`/sys/dev/block` (`/dev/root`), `stat('/').st_dev` (a btrfs root's anonymous device), every member of a multi-device
btrfs (`/sys/fs/btrfs/*/devices`), ZFS vdevs (`zpool status -P`), then up through md/dm/LVM/LUKS holders. A root
filesystem none of those can place makes EVERY disk busy and every disk controller unoffered — fail closed.
**Listed but refused, with the reason:** anything the host is using — a disk mounted anywhere (nas.lan's USB-SATA
bridge is a member of the md array under `/raid`), swap, a NIC (PCI or USB) whose interface is up, a USB controller
carrying the host's HID devices, the host's boot display GPU, a GPU on a host graphics driver (nvidia/amdgpu/i915/…),
and a device another VM holds (named). The owner scan FAILS CLOSED: a VM whose definition cannot be read refuses the
attach. Attach and detach hold a host-wide device lock, so two admins cannot give one stick to two VMs. Every attach
and detach is READ BACK from the live and the saved definitions; an attach that half-landed is rolled back and the
exact state reported.

**The checks run again at START.** `managed='yes'` takes a device from the host when the VM starts — possibly days
after the attach, and `vm.power` is a session op — so a start re-checks every saved hostdev (busy, host GPU driver,
IOMMU group, a card no longer on the host) and refuses with the reason. A saved USB entry is vendor:product, so the
check covers whichever present device matches it now.

**USB** hot-plugs into a running VM; "Keep attached after the VM restarts" adds `--config`. The saved form is
vendor/product with `startupPolicy='optional'` (a re-plug keeps working, a missing stick does not stop the VM
booting); bus/device is pinned only when two plugged-in devices share the ids.

**PCI** goes into a SHUT-OFF VM's definition with `managed='yes'`: libvirt (root) moves the card to vfio-pci when
the VM starts and back when it stops. A GPU brings its HDMI audio (function .1) and every other function in its IOMMU
group — never another device in the same slot (an AMD APU carries the board's own audio at .6). The picker shows
the checklist, each item measured, with the change to make: IOMMU on (`/sys/kernel/iommu_groups` not empty — else
`intel_iommu=on iommu=pt` / `amd_iommu=on iommu=pt` in `/etc/kernel/cmdline` or GRUB), vfio-pci available, the
whole IOMMU group can go (bridges may stay), not the host's display GPU, not held by a host driver (else
`/etc/modprobe.d/vfio.conf`: `options vfio-pci ids=…` + `softdep <driver> pre: vfio-pci`, rebuild the initramfs,
reboot), and — advisory — UEFI firmware and a q35 machine. Nothing here changes the kernel or the boot loader.

**QEMU must HAVE the device.** Measured on nas.lan: Gentoo builds `app-emulation/qemu` with `USE=-usb`, which leaves
out `usb-host`; libvirt accepted the attach and QEMU refused it ("'usb-host' is not a valid device model name"). The
host asks the emulator (`-device help`) and refuses with the fix before libvirt is asked. `./install.sh --vmhost`
checks it and, on Gentoo, writes `app-emulation/qemu usb` to `/etc/portage/package.use/posterchan-vmhost` and runs
`emerge --oneshot --changed-use app-emulation/qemu` (running VMs keep the old QEMU until restarted).

**Who opens the device node.** On `qemu:///system` libvirt runs as root and chowns `/dev/bus/usb/BBB/DDD` to the
qemu user for as long as the VM holds it, then gives it back — nothing to install. "This computer" uses
`qemu:///session`: libvirt runs as the signed-in account and cannot chown, so QEMU opens the node itself, as that
account, and the node is root-owned 0664. The grant is ONE DEVICE, AT ATTACH: `desktop/vmusb.js` runs
`sudo -n /usr/local/bin/pc-usb-grant grant BUS DEV`, which re-checks the device itself (the same scanner as the host,
installed as `/usr/local/lib/posterchan/pc_usb_scan.py`: no hubs, nothing the host uses) and puts an ACL for the
CALLER (SUDO_UID) on that one node; detach revokes it, and unplugging removes the node and the ACL. PosterChanOS ships
the helper with `%posterchan ALL=(root) NOPASSWD: …/pc-usb-grant grant *, …/pc-usb-grant revoke *` and builds QEMU
with `USE=usb`. A blanket udev `uaccess` rule was the first design and was withdrawn in review: it gave the seat raw
usbfs on every USB disk plugged in (driver disconnect, raw SCSI) — root in all but name.
PCI passthrough needs root and is a server-host feature; "This computer" says so.

## 7. The live probe (phase 4)

`scripts/vmhost_live_probe.py --storage <probe-only dir> [--capture tests/fixtures/vmhost_real]` runs the
shipped `VirshBackend` and `VmHostService` against the host's real libvirt, plus the real `/ws/vmconsole`
route on an in-process uvicorn. Every mutating virsh verb goes through one guard that refuses a uuid the probe
did not create and a definition not named `pcprobe-*`; everything it made is removed in a `finally` and
checked for leftovers. Exit 0 = every step passed, 1 = a check failed, 2 = could not run.

Steps: (1) host stats vs `/proc`; (2) create EFI + BIOS from generated XML; (3) start, VNC on 127.0.0.1 with
a `passwd`; (4) a console ticket's password opens a raw RFB connection (VNC auth offered, never None), a wrong
one fails, an expired one fails, a QMP error reply (virsh exit 0) raises, and ticket → `/ws/vmconsole` → RFB
authenticates through the real route while a reused ticket is refused; (5) reboot/shutdown/destroy; (6)
`vm.update` of every field incl. a disk and an ISO (generated in Python when no xorriso/genisoimage) read
back, and the VM starts with them; (7) offline snapshots — running refused, revert restores disk bytes
(checked with `qemu-io`) and variables; (8) `qemu-img info` on qcow2/raw/backing/data-file images; (9) the
migration primitives, with the target's REBUILT definition defined in a second storage root, booted and
console-authenticated, offline snapshots carried; (10) delete with disks, no leftovers.

**First run, nas.lan (libvirt 12.0.0, QEMU 10.2.3), 2026-09-16 — what it found:**

1. Every VM failed its first start: VM directories were 0750, QEMU runs as another user (§1 Storage).
2. `nvram.fd` was created by libvirt as `qemu:qemu 0600` and never returned — migration export raised
   PermissionError hashing it. The app now seeds it from libvirt's template.
3. `vm.delete` failed on a VM with snapshots: `undefine` needs `--snapshots-metadata`.
4. `dumpxml --migratable` drops `firmware='efi'` and spells out the SOURCE's loader path, so every target
   without that exact file (every Debian host) refused the precheck. The check is gone; the rebuild
   auto-selects this host's firmware.
5. The target never probed `nvram.fd` — with libvirt 12 running qcow2 varstores, a hostile qcow2 "varstore"
   naming a backing file was the same read-through hole as a hostile disk. Probed now, and its format pins
   `<nvram format>` so the target picks a matching firmware (raw 2M and qcow2 4M are not interchangeable).
6. libvirt 12 + qcow2 varstore snapshots a RUNNING EFI VM, contradicting the "EFI is refused" rule — the
   reason snapshots are offline now (§5a).
7. ACPI shutdown is ignored while a guest sits in firmware with no OS (expected; Force off is the fallback).

After the fixes every step passes. Still owed: a real two-host migration (server1 has no `/dev/kvm`).
