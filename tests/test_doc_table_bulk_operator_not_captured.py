"""The table copy signs with the operator key the node has NOW, never one captured at import time.

Deploy 205's gate failed tests/test_push_store.py::test_the_copied_rows_are_what_the_converted_paths_serve
(KeyError: 7 -- the copied push devices were not there) in the full suite AND in its serial retry, and passed in
every clean process. The cause was ORDER: `doc_table_bulk` did `from app.services.doc_table import _operator`, and
tests/doc_table_mem.mem_tables() patches `doc_table._operator` with a fake key and THEN imports doc_table_bulk.
When that was the process's first import (test_media_center ran before test_push_store in the retry), the fake
key was bound into doc_table_bulk for the rest of the process: every later copy wrote documents signed by a key
that is not the operator's, which the DocTable reading as the operator could not see. The same capture would make
a real copy sign with whatever key was current when the module happened to be imported.

Each case runs in a FRESH interpreter, because the bug only exists when the module is first imported there.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=120)


def test_a_copy_after_the_in_memory_tables_signs_with_the_real_operator_key():
    """mem_tables first (it imports doc_table_bulk under its patch), then undone: the copy must use the operator
    key doc_table resolves now, not the test's fake one."""
    r = _run("""
        import asyncio, sys
        assert "app.services.doc_table_bulk" not in sys.modules
        from _pytest.monkeypatch import MonkeyPatch
        from tests.doc_table_mem import mem_tables
        mp = MonkeyPatch()
        mem_tables(mp)
        mp.undo()

        from app.services import doc_table, doc_table_bulk
        real = (b"\\x07" * 32, "77" * 32)
        doc_table._operator = lambda: real          # the operator key this node has now
        seen = []
        async def list_all_docs(port, prefix, seckey=None, **kw):
            seen.append(seckey)
            return {}
        doc_table_bulk.nostr_store.list_all_docs = list_all_docs
        asyncio.run(doc_table_bulk.aread_all("push_subscriptions"))
        assert seen == [real[0]], "the copy read with %r, not the operator key" % (seen,)
        print("OK")
    """)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr

