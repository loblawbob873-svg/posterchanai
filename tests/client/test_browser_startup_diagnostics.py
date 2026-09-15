"""Chrome startup failures retain evidence and cannot masquerade as app regressions."""
import asyncio
import subprocess
import sys
import tempfile

import pytest

from tests.client.test_desktop_offline_full_app import wait_browser_port


@pytest.mark.parametrize('mode', ['delayed', 'crashed', 'stalled'])
def test_browser_port_startup_is_bounded_and_reports_process_errors(tmp_path, mode):
    port = tmp_path / 'DevToolsActivePort'
    script = {
        'delayed': "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]);p.write_text('');time.sleep(.15);p.write_text('12345\\n/devtools/browser/test');time.sleep(10)",
        'crashed': "import sys;sys.stderr.write('cannot initialize test browser');sys.exit(7)",
        'stalled': "import sys,time;sys.stderr.write('startup stalled\\n');sys.stderr.flush();time.sleep(10)",
    }[mode]
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen([sys.executable, '-c', script, str(port)], stdout=log, stderr=log)
        try:
            if mode == 'delayed':
                assert asyncio.run(wait_browser_port(proc, port, log, timeout=2)) == '12345'
            else:
                with pytest.raises(AssertionError, match='cannot initialize test browser' if mode == 'crashed' else 'startup stalled'):
                    asyncio.run(wait_browser_port(proc, port, log, timeout=.5))
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
