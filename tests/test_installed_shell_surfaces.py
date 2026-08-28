import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "installed_shell_surfaces", ROOT / "scripts" / "check_installed_shell_surfaces.py")
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def output(name, x):
    return {"name": name, "active": True, "rect": {"x": x, "y": 0, "width": 1920, "height": 1080}}


def surface(con_id, x, visible=True):
    return {"id": con_id, "app_id": "place.poster.desktop", "visible": visible,
            "rect": {"x": x, "y": 0, "width": 1920, "height": 1080}}


def tree(*nodes):
    return {"nodes": list(nodes), "floating_nodes": []}


def test_each_active_output_requires_one_exact_full_size_shell_surface():
    got = GATE.validate([output("DP-1", 0), output("DP-2", 1920)],
                        tree(surface(10, 0), surface(11, 1920)))
    assert got == {"outputs": 2, "surfaces": 2, "geometry": ["1920x1080", "1920x1080"]}


@pytest.mark.parametrize("nodes", [
    (surface(10, 0),),
    (surface(10, 0), surface(11, 0), surface(12, 1920)),
    (surface(10, 0), surface(11, 1920, visible=False)),
])
def test_missing_duplicate_or_invisible_companion_is_a_black_output_failure(nodes):
    with pytest.raises(RuntimeError, match="full visible shell surfaces"):
        GATE.validate([output("DP-1", 0), output("DP-2", 1920)], tree(*nodes))


def test_inactive_outputs_do_not_require_a_renderer():
    disconnected = output("DP-2", 1920)
    disconnected["active"] = False
    assert GATE.validate([output("DP-1", 0), disconnected], tree(surface(10, 0)))["outputs"] == 1


def test_headless_host_is_an_explicit_missing_prerequisite(monkeypatch):
    monkeypatch.setattr(GATE.subprocess, "run", lambda *args, **kwargs:
                        type("Result", (), {"stdout": "XDG_RUNTIME_DIR=/run/user/1\n"})())
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.delenv("I3SOCK", raising=False)
    with pytest.raises(GATE.PrerequisiteMissing, match="no installed Sway IPC session"):
        GATE.session_env()
