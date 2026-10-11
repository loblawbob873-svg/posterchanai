"""Nothing may retire a LIVE primary's marker.

2026-10-10 21:53: server1's relay (POSTERCHANDB_MODE=primary since 16:11) restarted in a deploy and refused to start --
"/var/lib/posterchandb/relay has no PRIMARY marker" -- a 2.5-minute outage of poster.place, ended by re-writing the
marker by hand. The relay itself never retires it while primary; `retire_marker` (called by thread._start_mirror
whenever a process starts on Postgres) removed it from under the running relay, from another process, with no check
that anything was using the directory. An open primary now holds a shared lock on its directory and retire_marker
refuses while any primary holds it -- in this process or another one.
"""
import asyncio
import os
import subprocess
import sys
import textwrap

from app.services.nostr_relay import pcdb_store as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _primary(tmp_path):
    d = str(tmp_path / "relay")
    P.init_new(d)
    loop = asyncio.new_event_loop()
    st = P.PcdbRelayStore(d, maintenance=False)
    st.open(loop)
    return st, loop, d


def test_another_process_cannot_retire_a_live_primary(tmp_path):
    st, loop, d = _primary(tmp_path)
    try:
        code = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            from app.services.nostr_relay import pcdb_store as P
            P.retire_marker(%r)
        """ % (ROOT, d))
        subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
        assert P.read_marker(d) is not None, "another process removed the marker of a running primary"
    finally:
        st.close()
        loop.close()


def test_the_relay_start_path_cannot_retire_it_either(tmp_path, monkeypatch):
    st, loop, d = _primary(tmp_path)
    monkeypatch.setenv("POSTERCHANDB_DIR", str(tmp_path))
    try:
        from app.services.nostr_relay import thread
        thread._start_mirror(object(), {"pcdb_mode": "off"})      # a Postgres-mode start, here or anywhere
        assert P.read_marker(d) is not None
    finally:
        st.close()
        loop.close()


def test_once_the_primary_is_closed_a_postgres_start_still_retires_it(tmp_path):
    st, loop, d = _primary(tmp_path)
    st.close()
    loop.close()
    P.retire_marker(d)
    assert P.read_marker(d) is None, "the deliberate retirement (a node going back to Postgres) stopped working"
