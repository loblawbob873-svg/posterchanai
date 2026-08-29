import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_desktop_restart_disposes_each_window_resource_exactly_once():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/window_dispose_runtime.mjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "window dispose runtime: ok" in run.stdout


def test_desktop_exit_uses_the_same_window_disposer_as_close():
    source = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    exit_body = source[source.index("function exit("):source.index("function toggle()")]
    assert "wins.slice().forEach(disposeWindow)" in exit_body
