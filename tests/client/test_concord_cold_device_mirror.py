"""A cold device reads this node's Concord copy at once (see the .mjs for the report)."""
import subprocess
from pathlib import Path


def test_a_cold_device_reads_this_nodes_copy_after_one_ask():
    r = subprocess.run(["node", str(Path(__file__).with_name("concord_cold_device_mirror_runtime.mjs"))],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_room_load_asks_the_node_before_its_first_read():
    """The seed is worthless if the room has already tunnelled: it runs before ensureMirror and the control read."""
    src = (Path(__file__).resolve().parents[2] / "static/js/client/concord.js").read_text()
    i = src.index("await seedMirrorFromNode(p);")
    assert i < src.index("ensureMirror(p,room,bundle,controlWraps)", i) < src.index("const controlRead=await queryControlUntilAsked", i)
    live = src[src.index("function startRoomsLive(p){"):][:200]
    assert "seedMirrorFromNode(p)" in live, "the background sweep must seed too (rooms not opened yet)"
