"""A failed OS update must not proceed to cleanup or rewrite the bootloader."""
import os
from pathlib import Path
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'os/gentoo.sh'


@pytest.mark.parametrize('failure,expected', [
    ('--sync', ['emerge --sync']),
    ('-uDN', ['emerge --sync', 'emerge -uDN @world']),
    ('-c', ['emerge --sync', 'emerge -uDN @world', 'emerge -c']),
    ('bootloader', ['emerge --sync', 'emerge -uDN @world', 'emerge -c', 'bootloader']),
    ('', ['emerge --sync', 'emerge -uDN @world', 'emerge -c', 'bootloader']),
])
def test_update_stops_at_the_first_failed_step(failure, expected):
    source = SOURCE.read_text()
    start = source.index('updateOS() {')
    function = source[start:source.index('\nconfigurePortage() {', start)]
    # Bash allows an absolute command name as a function. Intercept the actual calls without
    # rewriting their arguments or executing Portage on the machine running pytest.
    stubs = '''
/usr/bin/emerge() {
  echo "emerge $*"
  if [ "$1" = "$FAIL_STEP" ]; then return 23; fi
}
bootloader() {
  echo bootloader
  if [ "$FAIL_STEP" = bootloader ]; then return 23; fi
}
'''
    result = subprocess.run(['bash', '-c', stubs + function + '\nupdateOS\n'],
                            env={**os.environ, 'FAIL_STEP': failure},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == (23 if failure else 0), result.stdout + result.stderr
    assert result.stdout.splitlines() == expected
