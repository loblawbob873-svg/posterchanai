"""Real signed NIP09 events through production publish/repost/count/render and real Store."""
import subprocess
from pathlib import Path

def test_unboost_signed_delivery_ownership_and_hydration():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['node', str(Path(__file__).with_name('unboost_runtime.cjs'))], cwd=root, text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert '"passed":12' in result.stdout
