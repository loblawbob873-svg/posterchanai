from pathlib import Path
import subprocess

def test_verified_guestbook_projection():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['node', str(Path(__file__).with_name('concord_guestbook_runtime.mjs'))], cwd=root, text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr

def test_guestbook_async_ownership_and_ui_projection():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['node', str(Path(__file__).with_name('concord_guestbook_lifecycle_runtime.mjs'))], cwd=root, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
