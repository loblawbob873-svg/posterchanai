"""Run routine updates in Bash: stop on failure and never reprovision the boot chain."""
import os
from pathlib import Path
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'os/gentoo.sh'


STEPS = ['emerge --sync', 'policy', 'boot token', 'emerge -uDN @world',
         'emerge @preserved-rebuild', 'emerge -c']


@pytest.mark.parametrize('failure,expected', [
    ('--sync', STEPS[:1]),
    ('policy', STEPS[:2]),
    ('boot token', STEPS[:3]),
    ('-uDN', STEPS[:4]),
    ('@preserved-rebuild', STEPS[:5]),
    ('-c', STEPS),
    ('', STEPS),
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
prepareUpdateDependencies() {
  echo policy
  if [ "$FAIL_STEP" = policy ]; then return 23; fi
}
alignKernelEntryToken() {
  echo "boot token"
  if [ "$FAIL_STEP" = "boot token" ]; then return 23; fi
}
bootloader() {
  echo "ERROR: installer bootloader called" >&2
  return 99
}
decryptBoot() {
  echo "ERROR: disk key rotation called" >&2
  return 99
}
'''
    result = subprocess.run(['bash', '-c', stubs + function + '\nupdateOS\n'],
                            env={**os.environ, 'FAIL_STEP': failure},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == (23 if failure else 0), result.stdout + result.stderr
    assert result.stdout.splitlines() == expected
    assert result.stderr == ""
