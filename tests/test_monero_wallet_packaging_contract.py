"""WHAT THE MONERO WALLET NEEDS FROM THE IMAGE, THE INSTALLER AND requirements.txt.

The wallet added a service, a router, a client module and an admin tab, and it is easy to assume a
feature that size must have brought dependencies with it. It did not — and the value of writing that
down is that the next person does not add `monero-python` or a wallet binary to the image "to be
safe". The whole design is that PosterChan speaks JSON-RPC to a wallet daemon somebody else runs:

    * no new Python package — `httpx` was already a dependency, everything else is stdlib;
    * no `monero-wallet-rpc` in the image — it is opt-in, and on PosterChanOS it comes from its own
      ebuild (tests/test_monero_wallet_rpc_packaging.py covers that half);
    * no installer step — the wallet is off until an operator turns it on in Admin → Monero.

Two things that DO have to be true of the packaging, and both fail silently:

1. **The spend ledger has to land on a persisted volume.** It is the only durable record of what has
   been spent in the last 24 hours, and the daily cap is enforced from it. `WalletConfig.validate`
   refuses `:memory:` for exactly this reason — a ledger that dies with the process hands the whole
   daily allowance back on every restart. In Docker that means `/app/data` must be a volume, or the
   refusal is enforced against a path that is throwaway anyway.

2. **A container's `127.0.0.1` is the container's own loopback.** The default RPC URL points at
   localhost, which inside Docker is the app container — where no wallet daemon runs. A Docker
   operator has to point it at an RFC1918 address instead, so the validator must accept one. That
   used to be loopback-only, and a Docker deployment could not have reached a wallet at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app/services/monero_wallet_service.py"
REQS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")

STDLIB = {
    "__future__", "asyncio", "importlib", "ipaddress", "os", "secrets", "sqlite3", "time",
    "dataclasses", "decimal", "typing", "urllib", "json", "base64", "hashlib", "logging", "re",
    "contextlib", "functools", "math", "pathlib", "collections", "enum", "threading",
}


def _third_party_imports(path: Path):
    """Top-level modules the file imports that are neither stdlib nor our own package."""
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(?:import|from)\s+([A-Za-z_][\w.]*)", line)
        if not m:
            continue
        root = m.group(1).split(".")[0]
        if root in STDLIB or root == "app":
            continue
        out.add(root)
    return out


def test_the_wallet_service_needs_nothing_new_installed():
    """If this ever fails, the new import is a real packaging decision — add it to requirements.txt
    AND to the Dockerfile's install, rather than discovering it on a node that cannot start."""
    third_party = _third_party_imports(SERVICE)
    assert third_party <= {"httpx"}, f"the wallet service grew a dependency: {sorted(third_party)}"
    for pkg in sorted(third_party):
        assert re.search(rf"^{pkg}\b", REQS, re.M), f"{pkg} is imported but not in requirements.txt"


def test_the_router_needs_nothing_new_either():
    third_party = _third_party_imports(ROOT / "app/routers/monero_wallet.py")
    assert third_party <= {"fastapi", "pydantic"}, sorted(third_party)
    for pkg in sorted(third_party):
        assert re.search(rf"^{pkg}\b", REQS, re.M), f"{pkg} is not in requirements.txt"


def test_no_wallet_daemon_is_baked_into_the_image():
    """Deliberate: the node never holds spend keys, so it has no business shipping the thing that
    does. The daemon is the operator's, opt-in, and on PosterChanOS it has its own ebuild."""
    for token in ("monero-wallet-rpc", "monerod", "getmonero.org"):
        assert token not in DOCKERFILE, f"{token} is being installed into the image"
        assert token not in COMPOSE, f"{token} appears in docker-compose.yml"


def test_the_installer_has_no_monero_step_because_the_wallet_ships_off():
    """`monero_wallet_enabled` defaults off and the whole feature is configured in Admin → Monero,
    so a fresh install must not prompt for, install, or start anything."""
    assert not re.search(r"^\s*--monero\b", INSTALL, re.M), "install.sh grew a --monero mode"
    assert "monero-wallet-rpc" not in INSTALL


def test_the_spend_ledger_lands_on_a_persisted_volume_in_docker():
    """THE ONE THAT MATTERS FOR DOCKER. The rolling daily cap is enforced from this file and
    nothing else; `validate()` refuses `:memory:` precisely because a ledger that does not survive
    a restart hands the whole allowance back on every restart. That refusal is worth nothing if the
    directory it lives in is thrown away with the container."""
    default = re.search(r'"MONERO_WALLET_SPEND_LEDGER",\s*"([^"]+)"', SERVICE.read_text(encoding="utf-8"))
    assert default, "the spend ledger's default path has moved"
    path = default.group(1)
    assert path.startswith("data/"), f"the ledger defaults to {path!r}, outside the persisted data dir"
    assert ":/app/data" in COMPOSE, (
        "/app/data is no longer a docker volume — the spend ledger, and therefore the daily cap, "
        "would reset on every container restart")


def test_a_container_can_reach_a_wallet_on_the_operators_own_network():
    """A container's 127.0.0.1 is the CONTAINER. The default URL is localhost, where no wallet
    daemon runs inside the app image, so a Docker operator must point this at an RFC1918 address —
    which means the validator has to accept one. It was loopback-only at first, and under that rule
    a Docker deployment could not have reached a wallet at all."""
    from app.services.monero_wallet_service import MoneroWallet, WalletConfig, WalletError

    def cfg(url):
        return WalletConfig(enabled=True, url=url, username="u", password="p", network="stagenet",
                            transfer_cap_atomic=1, daily_cap_atomic=1, timeout_seconds=2,
                            spend_ledger_path="data/x.sqlite3")

    for reachable in ("http://192.168.0.85:38083/json_rpc", "http://10.1.2.3:38083/json_rpc",
                      "http://172.16.0.9:38083/json_rpc", "http://127.0.0.1:38083/json_rpc"):
        MoneroWallet(cfg(reachable))          # must not raise

    for refused in ("http://8.8.8.8:38083/json_rpc", "http://wallet.example.com:38083/json_rpc"):
        with pytest.raises(WalletError):
            MoneroWallet(cfg(refused))


def test_the_bare_metal_run_scripts_still_pick_up_the_operators_env():
    """The PosterChanOS helper writes MONERO_* into data/secrets.env; that only reaches the app
    because the run scripts source it. Docker deliberately does not — there is nothing to write it
    there, and the admin panel is the configured path on every deployment."""
    for script in ("run-intel.sh", "run-nvidia.sh"):
        text = (ROOT / script).read_text(encoding="utf-8")
        assert "data/secrets.env" in text, f"{script} no longer sources data/secrets.env"


def test_the_wallet_is_off_until_somebody_turns_it_on():
    """The packaging contract in one line: shipping this feature must change nothing on a node
    whose operator has not asked for it."""
    text = SERVICE.read_text(encoding="utf-8")
    enabled = re.search(r'"monero_wallet_enabled",\s*"MONERO_WALLET_ENABLED",\s*"([^"]*)"', text)
    assert enabled and enabled.group(1) == "", "the wallet no longer defaults to off"
