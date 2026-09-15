from pathlib import Path
import subprocess


def test_direct_invites_persist_without_link_and_merge_private_grants():
    result = subprocess.run(['node', str(Path(__file__).with_suffix('.mjs'))],
                            capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'direct membership passed' in result.stdout
