"""VM-hosting settings: ONE table of defaults, read by the service, the admin form test and the schema.

Every value is stored as a string (the settings store is key → string), so the typed accessors here
are the only place a value is interpreted. Blank means "use the default" for every key, and for the
two switches whose default is ON (`vmhost_announce`, `vmhost_iso_fetch_enabled`) a blank row must not
silently turn the feature off — the same trap `searxng_enabled` and `nip05_grants_access` document.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# key → default (string, exactly as SettingsResponse declares it). tests/test_vmhost_admin_settings.py
# asserts this table, SettingsResponse and the admin form agree on every key.
DEFAULTS: dict[str, str] = {
    "vmhost_enabled": "false",
    "vmhost_display_name": "",
    "vmhost_libvirt_uri": "qemu:///system",
    "vmhost_storage_dir": "/var/lib/posterchan/vms",
    "vmhost_public_url": "",
    "vmhost_public_relay": "",
    "vmhost_announce_relays": "",
    "vmhost_announce": "true",
    "vmhost_admin_npubs": "",
    "vmhost_allowed_npubs": "",
    "vmhost_peer_hosts": "",
    "vmhost_max_vcpus_per_vm": "16",
    "vmhost_max_ram_mib_per_vm": "65536",
    "vmhost_max_disk_gib_per_vm": "2048",
    "vmhost_reserve_ram_mib": "2048",
    "vmhost_reserve_disk_gib": "20",
    "vmhost_allow_overcommit": "false",
    "vmhost_default_network": "default",
    "vmhost_bridge": "",
    "vmhost_console_ticket_ttl_sec": "60",
    "vmhost_console_max_minutes": "240",
    "vmhost_shutdown_timeout_sec": "120",
    "vmhost_session_max_hours": "12",
    "vmhost_migration_keep_source_hours": "72",
    "vmhost_transfer_max_mbps": "0",
    "vmhost_iso_fetch_enabled": "true",
}

# Keys whose save must be DURABLE before the admin is told it worked: who may reach the host, and
# whether it runs at all. A best-effort background write that loses one of these reads back as the
# old access list on the next restart — a revoked user quietly regaining access.
DURABLE_KEYS = ("vmhost_enabled", "vmhost_admin_npubs", "vmhost_allowed_npubs")

def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _npub_list(raw: str) -> list:
    from app.services.nostr import nostr_service
    out, seen = [], set()
    for tok in str(raw or "").replace(",", "\n").split():
        pk = None
        # npub or 64-hex ONLY: decode_any would happily turn a pasted nsec into "a pubkey".
        low = tok.strip().lower()
        if not (low.startswith("npub1") or (len(low) == 64 and all(c in "0123456789abcdef" for c in low))):
            continue
        try:
            pk = nostr_service.to_pubkey_hex(tok.strip())
        except Exception:
            pk = None
        if pk:
            pk = pk.lower()
            if pk not in seen:
                seen.add(pk)
                out.append(pk)
    return out


def _int(v, default: str, lo: int, hi: int) -> int:
    try:
        n = int(str(v).strip() or default)
    except (TypeError, ValueError):
        n = int(default)
    return max(lo, min(hi, n))


@dataclass
class VmHostConfig:
    enabled: bool = False
    display_name: str = ""
    libvirt_uri: str = "qemu:///system"
    storage_dir: str = "/var/lib/posterchan/vms"
    public_url: str = ""
    public_relay: str = ""
    announce_relays: str = ""
    announce: bool = True
    admin_pubkeys: list = field(default_factory=list)
    allowed_pubkeys: list = field(default_factory=list)
    max_vcpus: int = 16
    max_ram_mib: int = 65536
    max_disk_gib: int = 2048
    reserve_ram_mib: int = 2048
    reserve_disk_gib: int = 20
    allow_overcommit: bool = False
    default_network: str = "default"
    bridge: str = ""
    ticket_ttl_sec: int = 60
    console_max_minutes: int = 240
    session_max_hours: int = 12
    iso_fetch_enabled: bool = True

    @classmethod
    def from_settings(cls, s: dict) -> "VmHostConfig":
        def g(key):
            v = s.get(key)
            v = "" if v is None else str(v)
            return v if v.strip() else DEFAULTS[key]

        def b(key):
            # A blank row falls back to the DEFAULT (via g), so the ON-by-default switches stay on.
            return _truthy(g(key))

        return cls(
            enabled=b("vmhost_enabled"),
            display_name=g("vmhost_display_name").strip(),
            libvirt_uri=g("vmhost_libvirt_uri").strip(),
            storage_dir=g("vmhost_storage_dir").strip(),
            public_url=g("vmhost_public_url").strip().rstrip("/"),
            public_relay=g("vmhost_public_relay").strip(),
            announce_relays=g("vmhost_announce_relays").strip(),
            announce=b("vmhost_announce"),
            admin_pubkeys=_npub_list(s.get("vmhost_admin_npubs", "")),
            allowed_pubkeys=_npub_list(s.get("vmhost_allowed_npubs", "")),
            max_vcpus=_int(g("vmhost_max_vcpus_per_vm"), DEFAULTS["vmhost_max_vcpus_per_vm"], 1, 512),
            max_ram_mib=_int(g("vmhost_max_ram_mib_per_vm"), DEFAULTS["vmhost_max_ram_mib_per_vm"], 256, 4 * 1024 * 1024),
            max_disk_gib=_int(g("vmhost_max_disk_gib_per_vm"), DEFAULTS["vmhost_max_disk_gib_per_vm"], 1, 1024 * 1024),
            reserve_ram_mib=_int(g("vmhost_reserve_ram_mib"), DEFAULTS["vmhost_reserve_ram_mib"], 0, 4 * 1024 * 1024),
            reserve_disk_gib=_int(g("vmhost_reserve_disk_gib"), DEFAULTS["vmhost_reserve_disk_gib"], 0, 1024 * 1024),
            allow_overcommit=b("vmhost_allow_overcommit"),
            default_network=g("vmhost_default_network").strip(),
            bridge=(s.get("vmhost_bridge") or "").strip(),
            ticket_ttl_sec=_int(g("vmhost_console_ticket_ttl_sec"), DEFAULTS["vmhost_console_ticket_ttl_sec"], 10, 600),
            console_max_minutes=_int(g("vmhost_console_max_minutes"), DEFAULTS["vmhost_console_max_minutes"], 1, 24 * 60),
            session_max_hours=_int(g("vmhost_session_max_hours"), DEFAULTS["vmhost_session_max_hours"], 1, 24 * 30),
            iso_fetch_enabled=b("vmhost_iso_fetch_enabled"),
        )


def current() -> VmHostConfig:
    from app.services import settings_store
    return VmHostConfig.from_settings(settings_store.all_settings())
