import importlib.util
from pathlib import Path


PATH = Path(__file__).parents[2] / "scripts/check_installed_native_focus.py"
SPEC = importlib.util.spec_from_file_location("native_focus_gate", PATH)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def node(app_id="pc-disposable-firefox", pid=42):
    return {"type": "output", "name": "DP-1", "nodes": [], "floating_nodes": [
        {"type": "floating_con", "app_id": app_id, "pid": pid, "nodes": [],
         "floating_nodes": [], "rect": {"x": 1, "y": 2, "width": 600, "height": 400}}
    ]}


def test_refuses_user_or_ambiguous_firefox_windows():
    for unsafe in ("firefox", "firefox-bin", ""):
        try:
            GATE.resolve(node(unsafe), unsafe, 42)
            assert False, "ordinary user Firefox was accepted"
        except ValueError:
            pass
    tree = node()
    tree["floating_nodes"].append(dict(tree["floating_nodes"][0]))
    try:
        GATE.resolve(tree, "pc-disposable-firefox", 42)
        assert False, "ambiguous target was accepted"
    except ValueError:
        pass


def test_resolves_exact_disposable_identity_and_output():
    found, output = GATE.resolve(node(), "pc-disposable-firefox", 42)
    assert found["pid"] == 42 and output == "DP-1"


def test_pixel_gate_distinguishes_black_flat_and_visible_content():
    def ppm(values):
        return b"P6\n2 2\n255\n" + bytes(values)
    mean, variance, nearblack = GATE.ppm_stats(ppm([0] * 12))
    assert mean == variance == 0 and nearblack == 1
    mean, variance, nearblack = GATE.ppm_stats(ppm([20, 80, 180] * 4))
    assert mean > 45 and variance > 250 and nearblack < .92
