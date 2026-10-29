"""A shard that hangs is reported with the test that hung.

Deploy 142 (2026-10-29): "shard 2 did not complete (pytest exit 124)", then 40 lines of a DocTable loader thread and
the pluggy frames from the end of the dump. faulthandler prints the MAIN thread -- the one running the test -- LAST,
below every helper thread, so the excerpt (the dump's first 40 lines + the log's last 15) never reached it, and a
third deploy run was spent with nothing to go on. The excerpt now also lists every tests/ frame anywhere in the dump.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gate", ROOT / "scripts/deploy_regression_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

HELPER = ['Thread 0x00007f82cdffb6c0 (most recent call first):'] + [
    f'  File "/usr/lib/python3.11/asyncio/base_events.py", line {n} in _run_once' for n in range(60)]
MAIN = ['Current thread 0x00007f82ef40f6c0 (most recent call first):',
        '  File "/usr/lib/python3.11/threading.py", line 327 in wait',
        '  File "/home/verita84/posterchanai/tests/test_relay_doc_reads.py", line 88 in test_a_read_that_never_answers',
        '  File "/home/verita84/posterchanai/venv-unified/lib/python3.11/site-packages/_pytest/python.py", line 159 in pytest_pyfunc_call'] + [
        f'  File "/venv/site-packages/pluggy/_callers.py", line {n} in _multicall' for n in range(30)]
OUTPUT = '\n'.join(['....................', 'Timeout (0:10:00)!'] + HELPER + MAIN)


def test_the_hung_test_is_named():
    text = gate._hang_excerpt(OUTPUT)
    assert "test_relay_doc_reads.py\", line 88 in test_a_read_that_never_answers" in text, text


def test_the_old_excerpt_could_not_name_it():
    lines = OUTPUT.strip().splitlines()
    dump = [i for i, l in enumerate(lines) if 'Timeout (' in l or 'most recent call first' in l]
    old = '\n'.join(lines[dump[0]:dump[0] + 40] + lines[-15:])
    assert "test_a_read_that_never_answers" not in old


def test_no_dump_is_the_tail():
    assert gate._hang_excerpt("\n".join(str(i) for i in range(100))).splitlines() == [str(i) for i in range(85, 100)]
