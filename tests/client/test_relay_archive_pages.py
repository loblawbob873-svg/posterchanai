"""Optional archive paging metadata uses verified per-relay answers, not union size."""
import subprocess
from pathlib import Path


def test_verified_relay_page_metadata():
    root=Path(__file__).resolve().parents[2]
    subprocess.run(['node',str(root/'tests/client/relay_archive_pages_runtime.mjs')],
                   cwd=root,check=True,timeout=15)
