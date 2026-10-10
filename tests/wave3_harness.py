"""The relay-only tables' test relay, for any test that drives code reading accounts, settings or bots (#161).

    from tests.wave3_harness import relay_mode, shared_relay  # noqa: F401  (fixtures)

Same harness as tests/wave2_harness.py (re-exported): a real RelayServer, a namespace per test, no SQL.
"""
from tests.wave2_harness import (fresh_process_view, no_relay, no_sql, relay_mode, shared_relay,  # noqa: F401
                                 use_relay)
