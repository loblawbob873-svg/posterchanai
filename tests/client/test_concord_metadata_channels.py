"""Independent signed metadata conversion and relay migration regression vectors."""
import pathlib
import subprocess


def test_concord_metadata_channels_runtime():
    root = pathlib.Path(__file__).resolve().parents[2]
    result = subprocess.run(['node', 'tests/client/concord_metadata_channels_runtime.mjs'], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'conversion history, granted keys and terminal channel deletion passed' in result.stdout
