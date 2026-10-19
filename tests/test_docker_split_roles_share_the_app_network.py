"""The Docker `split` layout must put every role in the app container's network namespace.

The app, the worker and the bot manager reach the relay at `ws://127.0.0.1:<port>/relay` (nostr_store, relay_reader,
~40 call sites) -- and since the Postgres store was deleted EVERY piece of state lives on that relay. In the split
layout the relay ran in its own container with its own loopback, so the app could read no settings, no users,
nothing (found by audit 2026-10-18; broken since 2026-08-02). Sharing the backend's network namespace makes
127.0.0.1 the same place for every role, without rewriting any of those call sites.
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_every_split_role_shares_the_backends_network():
    svc = _compose()["services"]
    split = [n for n, s in svc.items() if "split" in (s.get("profiles") or [])]
    assert {"relay", "worker", "media"} <= set(split), split
    for n in split:
        nm = str(svc[n].get("network_mode") or "")
        assert nm.startswith("service:"), "%s: own network namespace -> 127.0.0.1 is not the relay's" % n
        backend = nm.split(":", 1)[1]
        assert backend.startswith("${PC_SPLIT_BACKEND") or backend in svc, (n, nm)
        assert not svc[n].get("ports") and not svc[n].get("networks"), \
            "%s: a service in another's namespace cannot publish ports or join networks" % n


def test_the_split_backend_default_is_a_real_service():
    svc = _compose()["services"]
    nm = str(svc["relay"]["network_mode"])
    default = nm.split(":-", 1)[1].rstrip("}") if ":-" in nm else nm.split(":", 1)[1]
    assert default in svc, "network_mode names %r, which is not a service" % default
