"""The .onion address must be readable from a process that does not own the Tor daemon.

Admin → Network showed "starting… Tor is generating the address (a few seconds)" permanently, on a
deployment whose .onion had been live since June. Nothing was starting and nothing was generating:

  * tor runs in its OWN unit here (`run.py --role tor`, posterchanai-tor.service),
  * so in the APP process — the one serving /api/admin/onion — `_services` is empty,
  * `primary_service()` is therefore None and `get_onion_address()` returned None,
  * and the admin page renders exactly that string whenever `enabled` is true and `address` is null.

`set_onion()` already handled this case by reading the hostname file directly; the READ path never
did. The file is the truth either way — tor writes it, and it persists across restarts, which is what
makes the address stable in the first place.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import settings_store, tor_service  # noqa: E402

ADDR = "o2c7ssznoqr3xjfjtewxi2gerrbglckdm5y54lvsev4kv3ahjh2bf4qd.onion"


def _fake_datadir(tmp_path, address=None):
    d = tmp_path / "tor" / "onion_service"
    d.mkdir(parents=True)
    if address:
        (d / "hostname").write_text(address + "\n")
    return str(tmp_path / "tor")


def test_a_process_without_the_daemon_still_finds_the_address(tmp_path, monkeypatch):
    """THE regression. Every non-tor process — app, worker — is in this position on a role-split
    deployment, and the admin page is served by one of them."""
    data = _fake_datadir(tmp_path, ADDR)
    monkeypatch.setattr(tor_service, "_services", [])
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: data if k == "tor_data_dir" else d)
    assert tor_service.get_onion_address() == ADDR, (
        "a process that owns no tor daemon reported no .onion address; the admin page then claims "
        "Tor is still generating one, forever")


def test_no_hostname_file_is_still_none(tmp_path, monkeypatch):
    """The fallback must not invent an address: a genuinely fresh enable has no hostname file yet, and
    "generating…" is the correct thing to say for those few seconds."""
    data = _fake_datadir(tmp_path, None)
    monkeypatch.setattr(tor_service, "_services", [])
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: data if k == "tor_data_dir" else d)
    assert tor_service.get_onion_address() is None


def test_the_daemon_handle_still_wins_when_there_is_one(tmp_path, monkeypatch):
    """On the tor process itself the live handle is authoritative — the fallback is a fallback."""
    data = _fake_datadir(tmp_path, ADDR)

    class _Svc:
        def get_onion_address(self):
            return "fromhandle.onion"

    monkeypatch.setattr(tor_service, "_services", [_Svc()])
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: data if k == "tor_data_dir" else d)
    assert tor_service.get_onion_address() == "fromhandle.onion"
