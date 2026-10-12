"""Built-in Nostr web-of-trust relay (NIP-01/09/11), self-contained in PosterChanAI.

Runs in its own process (relay_main.py, see thread.py) and stores into PosterChanDB or Postgres; only
accepts/syncs events from a web-of-trust set. `osmode.py` is the same relay as PosterChanOS's loopback
event cache.

LAZY, on purpose: `start_nostr_relay`/`stop_nostr_relay` are resolved on first use. Importing them here
eagerly pulled thread.py -- the WoT crawl, the firehose, the settings store, git, VM hosting -- into every
importer of ANY module in this package, including the PosterChanOS local relay, which runs on the OS's own
Python with numpy and websockets and nothing else (tests/test_os_relay_import_closure.py).
"""

__all__ = ["start_nostr_relay", "stop_nostr_relay"]


def __getattr__(name):
    if name in __all__:
        from . import thread
        return getattr(thread, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
