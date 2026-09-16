# VM hosting (Virtual Machines)

PosterChan's own Proxmox-like VM hosting. A PosterChan **server node** with libvirt runs virtual
machines; admins create, delete and assign them; the people they are assigned to start, stop, reboot
and open their console — from the **Virtual Machines** screen of any PosterChan client (web, Android,
desktop, even a bundle with only a Nostr key and relays).

Management is **encrypted Nostr**, not an HTTP API: every operation is a signed kind-5310 event,
NIP-44-encrypted to the host node's key. The only thing that uses the node's HTTPS is the console
(noVNC), and it needs a one-use ticket that was itself issued over Nostr.

**Phase 1** (this document): host info, VM list/get, power, create from a library ISO, delete,
assign/unassign, console. Not yet: editing hardware, snapshots, ISO download/upload, session keys,
host discovery, cold migration.

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

Nothing new: the console socket is `/ws/vmconsole`, already covered by the stock
`location ^~ /ws/ { … }` block (3600s timeouts). Don't add a `proxy_set_header` inside it — see
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
and firehose (`app/services/vmhost/kinds.py`) apply special rules to NON-MEMBERS only — a web-of-trust
member's event of these kinds is treated exactly as it was before VM hosting (other apps use the
numbers too). A non-member's:

| Kind | Accepted only when |
|---|---|
| 5310 | this node runs a VM host (`vmhost_enabled`, applied live), the `p` tags are exactly `[this node]`, expiration ≤ now+600, `nofederate`, ≤ 70 KB |
| 6310 / 7310 | authored by this node's key (or a configured peer host — an empty hook until phase 3), with an expiration and `nofederate` |
| 31310 | authored by this node, a peer host or an operator |

Turning the host on adds a firehose subscription for 5310/6310/7310 `#p`-tagged to the node; switching
it and the rules above on or off is applied live via reload-upstream, no relay restart.

**No answer is not "no VMs".** An offline host and a host that drops strangers look identical to a
client; the UI says "No answer — the host is offline or you're not on its list" and keeps the last
known list on screen.

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
further tickets reuse its password, so two people sharing a VM do not lock each other out. noVNC 1.5.0 is vendored in `static/vendor/novnc/` (see its README).

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
Chrome, phone and desktop widths, real noVNC handshake drawing pixels).
