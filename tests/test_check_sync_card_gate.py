from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "check_sync_card.py").read_text(
    encoding="utf-8")


def test_preview_waits_for_the_shared_count_not_the_independent_local_count():
    """`0 here` may paint first; it is not evidence that the shared-count repaint landed."""
    start = SCRIPT.index("for(let i = 0; i < 10; i++)")
    block = SCRIPT[start:SCRIPT.index("/* The store chip", start)]
    assert "x.includes('in the folder')" in block
    assert "x.textContent.includes('in the folder')" in block
    assert "if(document.querySelector('.sync-counts span')) break" not in block


def test_preview_still_runs_the_real_dry_sweep_before_accepting_the_count():
    start = SCRIPT.index("for(let i = 0; i < 10; i++)")
    block = SCRIPT[start:SCRIPT.index("/* The store chip", start)]
    sweep = block.index("await window.PCSync.sweep(f, { manual:true, dryRun:true })")
    shared = block.index("x.includes('in the folder')")
    assert sweep < shared
