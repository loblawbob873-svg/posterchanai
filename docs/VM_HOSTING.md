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
the desktop app. Not yet: cold migration (phase 3).

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

Nothing new: the console socket is `/ws/vmconsole`, already covered by the stock
`location ^~ /ws/ { … }` block (3600s timeouts). Don't add a `proxy_set_header` inside it — see
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

A snapshot/update on a VM whose metadata records a migration (phase 3) is refused `migrating`.

Error codes: `bad_request forbidden not_found conflict busy insufficient_capacity unsupported
rate_limited backend_error timeout version internal migrating session_expired step_up_required`.

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

| Piece (phase 2) | Where |
|---|---|
| hardware edits (XML tree) | `domainxml.py` (`read_hardware`, `set_*`, `add_*`) |
| vm.update + snapshots | `app/services/vmhost/hardware.py` |
| ISO fetch/upload/delete | `app/services/vmhost/isolib.py`; PUT route in `app/routers/vmhost.py` |
| host.access | `app/services/vmhost/access.py` |
| session keys | `app/services/vmhost/sessions.py`; resolution in `transport.py` |
