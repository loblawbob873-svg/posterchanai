"""Host devices given to a VM — USB (hot-plug) and PCI (GPUs and other cards) — as ONE feature with a kind.

A mixin of `VmHostService` (service.py), like hardware.py. Three ops, every one ADMIN-only in the op table and
none of them a session op (a key sitting in localStorage must not be able to hand the host's hardware away):

  host.devices.list  {kind?, vm?}       what this host can give, per kind, with what stands in the way
  vm.device.attach   {vm, kind, …ids}    usb: vendor, product, bus?, device?, persist?   pci: address
  vm.device.detach   {vm, kind, …ids}

and `vm.get` answers `devices` — what the VM has now — for EVERY role that can see the VM (read only).

A KIND is a small class below (`UsbKind`, `PciKind`): how to scan the host, how to read a client's ids, how a
<hostdev> in a definition names a device, which XML gives one, and whether it can be hot-plugged. A new kind is
one class and one row in KINDS — no new op, no new client plumbing.

THE RULES EVERY KIND OBEYS:
  * Clients send IDS, never XML or paths; the device must exist on this host NOW (a fresh sysfs scan) and must
    not be something the host needs or is using (usb.py / pci.py decide, and say why).
  * A device another VM has — running, or saved in its definition — is refused, naming that VM.
  * The change is READ BACK: after `virsh attach-device` the live and/or saved definition must contain the
    device, and after a detach it must not. virsh saying 0 is not the verdict.
  * USB hot-plugs into a running VM (`--live`, plus `--config` when it should stay after a restart) and goes into
    a stopped VM's saved definition. PCI goes ONLY into a SHUT-OFF VM's definition (`--config`): libvirt hands the
    card to vfio-pci when the VM starts and gives it back when it stops (`managed='yes'`).
  * A VM that is migrating is refused (`_migration_guard`); a migration refuses a VM with any hostdev.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from . import pci, usb
from .backend import BackendError

logger = logging.getLogger(__name__)


def _err(code, msg):
    from .service import VmHostError
    return VmHostError(code, msg)


def _bool(args, key, default):
    v = args.get(key, default)
    if not isinstance(v, bool):
        raise _err("bad_request", f"{key} must be true or false")
    return v


class UsbKind:
    name = "usb"
    live = True

    def parse(self, args: dict) -> dict:
        extra = sorted(set(args) - {"vm", "kind", "vendor", "product", "bus", "device", "persist"})
        if extra:
            raise _err("bad_request", f"unknown field {extra[0]!r}")
        vendor, product = args.get("vendor"), args.get("product")
        if not (isinstance(vendor, str) and usb.HEX4.match(vendor) and isinstance(product, str)
                and usb.HEX4.match(product)):
            raise _err("bad_request", "vendor and product must each be four lowercase hex digits (e.g. 0781)")
        bus, dev = args.get("bus"), args.get("device")
        if (bus is None) != (dev is None):
            raise _err("bad_request", "give both bus and device, or neither")
        for k, v in (("bus", bus), ("device", dev)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 999):
                raise _err("bad_request", f"{k} must be a whole number from 1 to 999")
        return {"vendor": vendor, "product": product, "bus": bus, "device": dev}

    def entries(self, xml: str) -> list:
        return usb.hostdevs(xml)

    def entry_matches(self, entry: dict, dev) -> bool:
        return usb.matches(entry, dev)

    def spec_matches_entry(self, spec: dict, entry: dict) -> bool:
        """Does the client's identification name this attached entry?"""
        if entry.get("vendor") != spec["vendor"] or entry.get("product") != spec["product"]:
            return False
        if spec["bus"] is None or entry.get("bus") is None:
            return True
        return entry["bus"] == spec["bus"] and entry["device"] == spec["device"]

    def find(self, devices: list, spec: dict) -> list:
        return [d for d in devices if d.vendor == spec["vendor"] and d.product == spec["product"]
                and (spec["bus"] is None or (d.bus == spec["bus"] and d.device == spec["device"]))]

    def view(self, d, extra: dict) -> dict:
        v = d.view()
        v.update({"kind": "usb", "id": f"{d.vendor}:{d.product}@{d.bus}-{d.device}", "live": True})
        v.update(extra)
        return v

    @staticmethod
    def loose_key(e: dict) -> str:
        return f"{e.get('vendor')}:{e.get('product')}"

    def key(self, e: dict) -> str:
        """Two identical sticks are two rows: an entry that names its bus/device is keyed by them too."""
        k = self.loose_key(e)
        return k + (f"@{e['bus']}-{e['device']}" if e.get("bus") is not None and e.get("device") is not None else "")

    def entry_view(self, e: dict, devices: list) -> dict:
        host = next((d for d in devices if usb.matches(e, d)), None)
        label = host.label if host else f"USB device ({e.get('vendor') or '????'}:{e.get('product') or '????'})"
        return {"kind": "usb", "vendor": e.get("vendor"), "product": e.get("product"), "bus": e.get("bus"),
                "device": e.get("device"), "label": label, "present": host is not None, "key": self.key(e)}

    def xml_for(self, dev, devices: list) -> list:
        # vendor/product survives a re-plug (the bus/device numbers do not); the address is pinned only when
        # another plugged-in device has the same ids, where vendor/product alone would be ambiguous.
        twins = [d for d in devices if d.vendor == dev.vendor and d.product == dev.product]
        if len(twins) > 1:
            return [usb.hostdev_xml(dev.vendor, dev.product, dev.bus, dev.device)]
        return [usb.hostdev_xml(dev.vendor, dev.product)]

    def detach_xml(self, entry: dict) -> str:
        # libvirt matches a USB hostdev by bus/device when the element carries them, else by vendor/product.
        if entry.get("vendor") and entry.get("product"):
            return usb.hostdev_xml(entry["vendor"], entry["product"], entry.get("bus"), entry.get("device"),
                                   optional=False)
        raise _err("unsupported", "this USB device is attached by address only — detach it with virsh")


class PciKind:
    name = "pci"
    live = False

    def parse(self, args: dict) -> dict:
        extra = sorted(set(args) - {"vm", "kind", "address"})
        if extra:
            raise _err("bad_request", f"unknown field {extra[0]!r}")
        a = args.get("address")
        if not (isinstance(a, str) and pci.ADDR.match(a)):
            raise _err("bad_request", "address must be a PCI address like 0000:01:00.0")
        return {"address": a}

    def entries(self, xml: str) -> list:
        return pci.hostdevs(xml)

    def entry_matches(self, entry: dict, dev) -> bool:
        return entry.get("address") == dev.address

    def spec_matches_entry(self, spec: dict, entry: dict) -> bool:
        return entry.get("address") == spec["address"]

    def find(self, devices: list, spec: dict) -> list:
        return [d for d in devices if d.address == spec["address"] and not d.hidden and not d.system]

    def view(self, d, extra: dict) -> dict:
        v = {"kind": "pci", "id": d.address, "address": d.address, "vendor": d.vendor, "product": d.product,
             "label": d.label, "class": d.cls, "class_name": d.class_name(), "driver": d.driver, "group": d.group,
             "busy": d.busy, "gpu": d.is_gpu, "live": False}
        v.update(extra)
        return v

    @staticmethod
    def key(e: dict) -> str:
        return e["address"]

    loose_key = key

    def entry_view(self, e: dict, devices: list) -> dict:
        host = next((d for d in devices if d.address == e["address"]), None)
        return {"kind": "pci", "address": e["address"], "label": host.label if host else f"PCI device {e['address']}",
                "present": host is not None, "key": e["address"]}

    def detach_xml(self, entry: dict) -> str:
        return pci.hostdev_xml(entry["address"])


KINDS = {"usb": UsbKind(), "pci": PciKind()}


def _classify(e: BackendError):
    """libvirt's refusals as the error codes a client can act on."""
    msg = str(e)
    low = msg.lower()
    if "in use by" in low or "already in use" in low:
        return _err("conflict", msg)
    if "did not find usb device" in low or "no such file" in low and "/dev/bus/usb" in low:
        return _err("not_found", "the device is not plugged into this host (any more): " + msg)
    if "permission denied" in low or "operation not permitted" in low:
        return _err("backend_error", "the host would not let QEMU open the device (" + msg + ") — see "
                                     "docs/VM_HOSTING.md, \"Devices\"")
    return e


class DeviceOps:
    # ------------------------------------------------------------------------------ helpers
    @staticmethod
    def _kind(args) -> "UsbKind | PciKind":
        k = KINDS.get(args.get("kind")) if isinstance(args.get("kind"), str) else None
        if k is None:
            raise _err("bad_request", "kind must be one of: " + ", ".join(sorted(KINDS)))
        return k

    async def _scan(self, kind: str) -> list:
        f = getattr(self.backend, "host_devices", None)
        if f is None:
            raise _err("unsupported", "this host cannot list its devices")
        try:
            return list(await f(kind))
        except FileNotFoundError:
            raise _err("unsupported", f"this host has no {kind.upper()} bus")

    async def _xmls(self, d) -> tuple:
        """(live XML or None, saved XML) of a domain. Live only when it runs — that is where a hot-plugged
        device that is not kept after a restart lives."""
        saved = await self.backend.dumpxml(d.uuid, inactive=True)
        live = await self.backend.dumpxml(d.uuid, inactive=False) if d.state in ("running", "paused") else None
        return live, saved

    async def _owners(self, kind) -> list:
        """[(entry, domain)] for every device of this kind any VM on the host holds, live or saved."""
        out = []
        for d in await self.backend.list_domains():
            try:
                live, saved = await self._xmls(d)
            except BackendError as e:
                if re.search(r"domain not found|failed to get domain|no domain", str(e), re.I):
                    continue                              # gone since the listing: it holds nothing
                # FAIL CLOSED: a VM whose definition cannot be read may hold the very device being handed out
                raise _err("backend_error", f"could not read the devices of the VM {d.name} ({e}) — refusing, so "
                                            "one device is never given to two VMs")
            for x in (live, saved):
                if x:
                    out.extend((e, d) for e in kind.entries(x))
        return out

    async def _vm_devices(self, d) -> list:
        """What a VM has: [{kind, label, live, persistent, present, …}] — for vm.get, every role."""
        try:
            live, saved = await self._xmls(d)
        except BackendError:
            return []
        out = []
        for kname, kind in KINDS.items():
            if not any(kind.entries(x) for x in (saved, live) if x):
                continue                                  # no sysfs walk for a kind the VM has none of
            try:
                devices = await self._scan(kname)
            except Exception:
                devices = []
            seen = {}
            for e in kind.entries(saved or ""):
                v = kind.entry_view(e, devices)
                v.update({"live": False, "persistent": True})
                seen[kind.key(e)] = v
            for e in kind.entries(live or ""):
                # a running VM reports the bus/device libvirt resolved: it belongs to the saved entry that pinned
                # exactly that address, else to an unpinned saved entry with the same ids not yet matched
                k = kind.key(e)
                if k not in seen:
                    loose = kind.loose_key(e)
                    if loose in seen and not seen[loose]["live"]:
                        k = loose
                cur = seen.get(k)
                if cur is None:
                    cur = seen[k] = kind.entry_view(e, devices)
                    cur.update({"live": False, "persistent": False})
                cur["live"] = True
                if e.get("bus") is not None and cur.get("bus") is None:
                    cur["bus"], cur["device"] = e["bus"], e["device"]
            out.extend(seen.values())
        return out

    # ------------------------------------------------------------------------------ host.devices.list
    async def _op_host_devices_list(self, pk, role, args, progress):
        want = args.get("kind")
        kinds = [self._kind(args).name] if want is not None else list(KINDS)
        d = await self._domain(pk, role, args) if args.get("vm") is not None else None
        owners_cache = {}
        out = {}
        for kname in kinds:
            kind = KINDS[kname]
            entry = {"live": kind.live, "devices": [], "checks": await self._host_checks(kname), "error": ""}
            try:
                devices = await self._scan(kname)
            except Exception as e:
                entry["error"] = getattr(e, "message", None) or str(e)
                out[kname] = entry
                continue
            owners = owners_cache.get(kname)
            if owners is None:
                try:
                    owners = owners_cache[kname] = await self._owners(kind)
                except Exception as e:
                    entry["error"] = getattr(e, "message", None) or str(e)
                    out[kname] = entry
                    continue

            def used_by(dev):
                for e, od in owners:
                    if kind.entry_matches(e, dev):
                        return {"uuid": od.uuid, "name": od.name}
                return None
            checks = entry["checks"]
            if kname == "usb":
                entry["devices"] = [kind.view(x, {"used_by": used_by(x)}) for x in usb.offered(devices)]
            else:
                groups = {}
                for x in devices:
                    if x.group:
                        groups.setdefault(x.group, []).append(x.address)
                rows = []
                for x in devices:
                    if x.hidden or x.system:
                        continue
                    p = pci.plan(x, devices, groups)
                    rows.append(kind.view(x, {"used_by": used_by(x), "checks": p["checks"],
                                              "with": [t.address for t in p["attach"][1:]],
                                              "passable": not p["blockers"] and all(c["ok"] for c in checks),
                                              "blockers": p["blockers"]}))
                entry["devices"] = rows
                if d is not None:
                    try:
                        entry["vm_checks"] = pci.vm_checks(await self.backend.dumpxml(d.uuid, inactive=True))
                    except BackendError:
                        entry["vm_checks"] = []
            out[kname] = entry
        return {"kinds": out}

    async def _device_start_guard(self, d) -> None:
        """Re-run the device safety checks for every <hostdev> in a VM's SAVED definition before it starts. The checks
        at attach time are not enough: `managed='yes'` takes the device from the host when the VM STARTS — days
        later, perhaps, when the saved stick's vendor:product now matches the host's backup disk, or the card the
        VM was given is on the nvidia driver the app computes with. vm.power start is a session op, so this is the
        only place those checks can still happen."""
        if getattr(self.backend, "host_devices", None) is None:
            return
        try:
            saved = await self.backend.dumpxml(d.uuid, inactive=True)
        except BackendError as e:
            raise _err("backend_error", f"could not read this VM's devices before starting it: {e}")
        usb_e, pci_e = usb.hostdevs(saved), pci.hostdevs(saved)
        if usb_e:
            try:
                devices = await self._scan("usb")
            except Exception:
                devices = []
            for e in usb_e:
                for dev in devices:
                    if usb.matches(e, dev) and (dev.system or dev.busy or dev.hub):
                        why = dev.busy or "the host boots from it"
                        raise _err("conflict", f"this VM's saved devices include {dev.label}, which the host is using "
                                               f"({why}) — detach it from the VM, or stop using it on the host, first")
        if pci_e:
            devices = await self._scan("pci")
            bad = [c for c in await self._host_checks("pci") if not c["ok"]]
            if bad:
                raise _err("unsupported", "this VM has PCI devices, but " + bad[0]["label"] + ". " + bad[0]["fix"])
            by = {x.address: x for x in devices}
            mine = {e["address"] for e in pci_e}
            for a in sorted(mine):
                dev = by.get(a)
                if dev is None:
                    raise _err("conflict", f"the PCI device {a} saved in this VM is not on this host any more — "
                                           "detach it from the VM first")
                if dev.system or dev.busy:
                    raise _err("conflict", f"this VM's saved devices include {dev.label}, which the host is using "
                                           f"({dev.busy or 'the host boots from it'}) — detach it or free it first")
                for m in (x for x in devices if x.group and x.group == dev.group):
                    if m.address not in mine and not m.cls.startswith("06") and m.driver not in pci.FREE_DRIVERS:
                        raise _err("conflict", f"{dev.label} is in IOMMU group {dev.group} with {m.address} ({m.label}, "
                                               f"{m.driver}), which the host uses — a VM gets a whole group or nothing")

    async def _host_checks(self, kind: str) -> list:
        f = getattr(self.backend, "device_checks", None)
        if f is None:
            return []
        try:
            return list(await f(kind))
        except Exception as e:
            logger.debug("[vmhost] %s host checks failed: %s", kind, e)
            return []

    # ------------------------------------------------------------------------------ attach / detach
    def _devices_lock(self) -> asyncio.Lock:
        lk = getattr(self, "_dev_lock", None)
        if lk is None:
            lk = self._dev_lock = asyncio.Lock()
        return lk

    async def _device_locked(self, pk, role, args):
        """(domain, release) with the HOST-WIDE device lock and the VM lock held, in that order: two admins giving
        one stick to two VMs at once must not both pass the "nobody has it" check before either attaches."""
        d = await self._domain(pk, role, args)
        dlock = self._devices_lock()
        await self._acquire(dlock, "this host's devices")
        lock = self._vm_lock(d.uuid)
        try:
            await self._acquire(lock, "this VM")
        except BaseException:
            dlock.release()
            raise

        def release():
            lock.release()
            dlock.release()
        try:
            d = await self.backend.get(d.uuid)
            if d is None or not self._visible(d, role, pk):
                raise _err("not_found", "no such VM")
            self._migration_guard(d)
        except BaseException:
            release()
            raise
        return d, release

    async def _virsh_device(self, verb: str, d, xml: str, live: bool, config: bool) -> None:
        tmpdir = self.storage.state_dir / "devices"

        def write() -> str:
            os.makedirs(tmpdir, mode=0o700, exist_ok=True)
            path = os.path.join(tmpdir, f"{d.uuid}-{os.getpid()}-{id(xml)}.xml")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(xml)
            return path
        path = await asyncio.to_thread(write)
        try:
            fn = getattr(self.backend, verb + "_device")
            await fn(d.uuid, path, live=live, config=config)
        except BackendError as e:
            raise _classify(e)
        finally:
            await asyncio.to_thread(lambda: os.path.exists(path) and os.unlink(path))

    async def _op_vm_device_attach(self, pk, role, args, progress):
        kind = self._kind(args)
        spec = kind.parse(args)
        persist = _bool(args, "persist", True) if kind.name == "usb" else True
        d, release = await self._device_locked(pk, role, args)
        try:
            running = d.state in ("running", "paused")
            if kind.name == "pci" and d.state != "shutoff":
                raise _err("conflict", "shut the VM down to add a PCI device — a card cannot be plugged into a "
                                       "running VM; it is handed over when the VM starts")
            if kind.name == "usb" and not running and d.state != "shutoff":
                raise _err("conflict", f"the VM is {d.state} — try again in a moment")
            if not running and not persist:
                raise _err("bad_request", "the VM is not running — a device added now is kept in its settings, so "
                                          "send persist: true")
            devices = await self._scan(kind.name)
            found = kind.find(devices, spec)
            if kind.name == "usb":
                found = [x for x in usb.offered(found)]
            if not found:
                raise _err("not_found", "that device is not on this host (any more)")
            if len(found) > 1:
                raise _err("bad_request", "more than one device has these ids — name it by bus and device too")
            dev = found[0]
            bad = [c for c in await self._host_checks(kind.name) if not c["ok"]]
            if bad:
                raise _err("unsupported", bad[0]["label"] + ". " + bad[0]["fix"])
            if kind.name == "usb":
                if dev.busy:
                    raise _err("conflict", f"{dev.label} is in use by the host: {dev.busy}")
                take, xmls = [dev], kind.xml_for(dev, devices)
            else:
                groups = {}
                for x in devices:
                    if x.group:
                        groups.setdefault(x.group, []).append(x.address)
                p = pci.plan(dev, devices, groups)
                if p["blockers"]:
                    fix = next((c["fix"] for c in p["checks"] if not c["ok"] and c["fix"]), "")
                    raise _err("conflict", f"{dev.label} cannot be given to a VM: " + "; ".join(p["blockers"]) +
                               (". " + fix if fix else ""))
                take = p["attach"]
                xmls = [pci.hostdev_xml(t.address) for t in take]
            for e, od in await self._owners(kind):
                for t in take:
                    if kind.entry_matches(e, t):
                        if od.uuid == d.uuid:
                            raise _err("conflict", f"{t.label} is already attached to this VM")
                        raise _err("conflict", f"{t.label} is already attached to the VM {od.name}")
            live = running and kind.live
            config = persist or not running
            if progress:
                await progress({"phase": "attach", "msg": "attaching " + dev.label})
            done = []
            try:
                for x in xmls:
                    await self._virsh_device("attach", d, x, live, config)
                    done.append(x)
            except BaseException:
                for x in done:          # a GPU without its audio half is worse than neither
                    try:
                        await self._virsh_device("detach", d, x, live, config)
                    except Exception as e:
                        logger.warning("[vmhost] rollback of a device attach on %s failed: %s", d.uuid, e)
                raise
            # READ BACK: the definition(s) we changed must now hold every device.
            now_live, now_saved = await self._xmls(d)
            missing = []
            for t in take:
                if live and not any(kind.entry_matches(e, t) for e in kind.entries(now_live or "")):
                    missing.append(f"{t.label} (in the running VM)")
                if config and not any(kind.entry_matches(e, t) for e in kind.entries(now_saved or "")):
                    missing.append(f"{t.label} (in its saved settings)")
            if missing:
                # Take back what DID land, so a half-attached device is never left behind, then say exactly what
                # the VM holds now — "failed" alone would hide a device the VM may already be using.
                undo = []
                for x in xmls:
                    for lv, cf in ((live, False), (False, config)):
                        if not (lv or cf):
                            continue
                        try:
                            await self._virsh_device("detach", d, x, lv, cf)
                        except Exception as e:
                            undo.append(str(getattr(e, "message", e))[:120])
                after_live, after_saved = await self._xmls(d)
                left = []
                for t in take:
                    if any(kind.entry_matches(e, t) for e in kind.entries(after_live or "")):
                        left.append(f"{t.label} (in the running VM)")
                    if any(kind.entry_matches(e, t) for e in kind.entries(after_saved or "")):
                        left.append(f"{t.label} (in its saved settings)")
                state = ("the part that landed was removed again, so the VM does not have it" if not left else
                         "removing the part that landed did not fully work — the VM still has: " + ", ".join(left) +
                         (" (" + "; ".join(undo) + ")" if undo else ""))
                raise _err("backend_error", "the host accepted the attach but did not keep: " + ", ".join(missing) +
                           "; " + state)
            logger.info("[vmhost] attached %s %s to VM %s (%s) live=%s config=%s by %s", kind.name,
                        ", ".join(t.label for t in take), d.name, d.uuid, live, config, pk[:12])
            view = self._vm_view(d, role, pk)
            view["devices"] = await self._vm_devices(d)
            return {"vm": view}
        finally:
            release()

    @staticmethod
    def _pci_detach_set(address: str, saved: list, devices: list) -> list:
        """The saved PCI entries that leave together — the SAME rule attach used (pci.plan), from whichever card
        `address` belongs to: detaching a GPU takes its audio; detaching its .1 audio takes the GPU with it; a
        device in the same slot that is not the card's (an APU's board audio at .6) stays. A card that is no longer
        on the host cannot be planned, so every saved function of its slot leaves with it (nothing left behind)."""
        by = {x.address: x for x in devices}
        groups = {}
        for x in devices:
            if x.group:
                groups.setdefault(x.group, []).append(x.address)
        saved_addrs = {e["address"] for e in saved}
        dev = by.get(address)
        if dev is None:
            slot = address.rsplit(".", 1)[0]
            return [e for e in saved if e["address"].rsplit(".", 1)[0] == slot]
        owner = dev
        if not dev.is_gpu:
            for g in devices:
                if g.is_gpu and g.slot == dev.slot and g.address in saved_addrs and \
                        address in {t.address for t in pci.plan(g, devices, groups)["attach"]}:
                    owner = g
                    break
        want = {t.address for t in pci.plan(owner, devices, groups)["attach"]} if owner.is_gpu else {address}
        return [e for e in saved if e["address"] in want]

    async def _op_vm_device_detach(self, pk, role, args, progress):
        kind = self._kind(args)
        spec = kind.parse(args)
        d, release = await self._device_locked(pk, role, args)
        try:
            running = d.state in ("running", "paused")
            if kind.name == "pci" and d.state != "shutoff":
                raise _err("conflict", "shut the VM down to remove a PCI device")
            live_xml, saved_xml = await self._xmls(d)
            live_hits = [e for e in kind.entries(live_xml or "") if kind.spec_matches_entry(spec, e)] if running else []
            saved_hits = [e for e in kind.entries(saved_xml or "") if kind.spec_matches_entry(spec, e)]
            if not live_hits and not saved_hits:
                raise _err("not_found", "that device is not attached to this VM")
            if len(live_hits) > 1 or len(saved_hits) > 1:
                raise _err("bad_request", "more than one attached device has these ids — name it by bus and device")
            targets = []
            if kind.name == "pci":
                saved_hits = self._pci_detach_set(spec["address"], kind.entries(saved_xml or ""),
                                                  await self._scan("pci"))
            if live_hits:
                targets.append((kind.detach_xml(live_hits[0]), True, False))
            for e in saved_hits:
                targets.append((kind.detach_xml(e), False, True))
            if progress:
                await progress({"phase": "detach", "msg": "detaching the device"})
            for x, lv, cf in targets:
                await self._virsh_device("detach", d, x, lv, cf)
            now_live, now_saved = await self._xmls(d)
            gone_addrs = {e.get("address") for e in saved_hits}

            def still(xml):
                for e in kind.entries(xml or ""):
                    if kind.name == "pci" and e.get("address") in gone_addrs:
                        return True
                    if kind.name != "pci" and kind.spec_matches_entry(spec, e):
                        return True
                return False
            left = []
            if live_hits and still(now_live):
                left.append("the running VM")
            if saved_hits and still(now_saved):
                left.append("its saved settings")
            if left:
                raise _err("backend_error", "the host accepted the detach but the device is still in " +
                           " and ".join(left))
            logger.info("[vmhost] detached %s %s from VM %s (%s) by %s", kind.name,
                        re.sub(r"[^0-9a-f:.@-]", "", str(spec)), d.name, d.uuid, pk[:12])
            view = self._vm_view(d, role, pk)
            view["devices"] = await self._vm_devices(d)
            return {"vm": view}
        finally:
            release()

