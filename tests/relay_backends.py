"""The relay store tests run against BOTH backends: RelayStore (Postgres) and PcdbRelayStore (PosterChanDB alone,
POSTERCHANDB_MODE=primary, task #161). A test that holds for one store and not the other is a rule the
promotion would silently drop, so every store_factory fixture is parametrized over BACKENDS.

The PosterChanDB side needs no server, so it never skips when Postgres is absent."""
from __future__ import annotations

import contextlib
import shutil
import tempfile

BACKENDS = ("postgres", "pcdb")


@contextlib.contextmanager
def pcdb_factory():
    from app.services.nostr_relay.pcdb_store import PcdbRelayStore, init_new
    made, dirs = [], []

    def _make(loop, **kw):
        d = tempfile.mkdtemp(prefix="pc-pcdb-relay-")
        dirs.append(d)
        init_new(d)
        st = PcdbRelayStore(d, maintenance=False, **kw)
        st.open(loop)
        made.append(st)
        return st

    try:
        yield _make
    finally:
        for st in made:
            try:
                st.close()
            except Exception:      # noqa: BLE001
                pass
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


def is_pcdb(store) -> bool:
    from app.services.nostr_relay.pcdb_store import PcdbRelayStore
    return isinstance(store, PcdbRelayStore)
