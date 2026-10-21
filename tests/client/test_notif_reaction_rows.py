"""Grouped reactions are drawn as Amethyst does -- see notif_reaction_rows_runtime.mjs."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_reactions_on_one_post_are_rows_of_faces_per_reaction():
    r = subprocess.run(["node", "tests/client/notif_reaction_rows_runtime.mjs"], cwd=ROOT, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr
