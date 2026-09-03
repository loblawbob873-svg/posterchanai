import json
import os
import socket
import struct
import subprocess
import threading
import time
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "os/bin/pc-wayfire-health"
LAUNCHER = ROOT / "os/bin/pc-shell-start-wayfire"


class WayfireStub:
    def __init__(self, path, outputs, views):
        self.path = str(path)
        self.outputs = outputs
        self.views = views
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen()
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            with client:
                raw = client.recv(4)
                if len(raw) != 4:
                    continue
                size = struct.unpack("<I", raw)[0]
                body = b""
                while len(body) < size:
                    body += client.recv(size - len(body))
                method = json.loads(body)["method"]
                if method == "list-methods":
                    result = {"methods": ["window-rules/list-outputs", "window-rules/list-views",
                                           "window-rules/configure-view", "window-rules/focus-view"]}
                elif method == "window-rules/list-outputs":
                    result = self.outputs
                else:
                    result = self.views
                payload = json.dumps(result).encode()
                client.sendall(struct.pack("<I", len(payload)) + payload)

    def close(self):
        self.stop = True
        self.sock.close()
        try:
            socket.socket(socket.AF_UNIX).connect(self.path)
        except OSError:
            pass
        self.thread.join(timeout=1)


def test_bundle_gate_rejects_the_old_sway_only_desktop(tmp_path):
    asar = tmp_path / "app.asar"
    asar.write_bytes(b'{"files":{"wm.js":{}}}')
    done = subprocess.run([str(HEALTH), "bundle"], env=os.environ | {"PC_DESKTOP_ASAR": str(asar)},
                          text=True, capture_output=True)
    assert done.returncode == 1
    assert "lacks the Wayfire backend" in done.stderr
    asar.write_bytes(b'{"files":{"wm-wayfire.js":{}}}')
    assert subprocess.run([str(HEALTH), "bundle"],
                          env=os.environ | {"PC_DESKTOP_ASAR": str(asar)}).returncode == 0


MARKER = [(209, 46, 145), (35, 205, 232), (121, 212, 71), (240, 180, 41)]


def _png(path, marker=True, transparent=False):
    width = height = 32
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            rgb = (0, 0, 0)
            if marker and 1 <= x < 9 and 1 <= y < 9:
                rgb = MARKER[(y >= 5) * 2 + (x >= 5)]
            pixels.extend((*rgb, 0 if transparent else 255))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
                     chunk(b"IDAT", zlib.compress(bytes(pixels))) + chunk(b"IEND", b""))


def _grim(tmp_path, captures):
    bindir = tmp_path / "bin"; bindir.mkdir(exist_ok=True)
    tool = bindir / "grim"
    mapping = tmp_path / "captures"
    mapping.mkdir(exist_ok=True)
    for name, source in captures.items():
        (mapping / name).write_bytes(source.read_bytes())
    _fake(tool, f'cp "{mapping}/$2" "$3"\n')
    return tool


def test_health_requires_exactly_one_rendered_owned_surface_per_output(tmp_path):
    asar = tmp_path / "app.asar"
    asar.write_bytes(b'wm-wayfire.js')
    outputs = [{"id": 1, "name": "DP-1"}, {"id": 2, "name": "DP-2"}]
    views = [{"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1},
             {"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 2}]
    stub = WayfireStub(tmp_path / "wayfire.socket", outputs, views)
    visible = tmp_path / "visible.png"; _png(visible)
    grim = _grim(tmp_path, {"DP-1": visible, "DP-2": visible})
    env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar),
                        "PC_GRIM": str(grim), "PC_GRIM_TIMEOUT": ".05"}
    try:
        assert subprocess.run([str(HEALTH), "preflight"], env=env).returncode == 0
        assert subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".3"], env=env).returncode == 0
        stub.views.append(dict(views[0]))
        failed = subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".2"], env=env,
                                text=True, capture_output=True)
        assert failed.returncode == 1
        assert "verified shell surface on every output" in failed.stderr
    finally:
        stub.close()


def test_mapped_shell_with_black_transparent_or_stale_output_fails_visual_gate(tmp_path):
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    visible = tmp_path / "visible.png"; black = tmp_path / "black.png"; transparent = tmp_path / "transparent.png"
    _png(visible); _png(black, marker=False); _png(transparent, marker=True, transparent=True)
    outputs = [{"id": 1, "name": "DP-1"}, {"id": 2, "name": "DP-2"}]
    views = [{"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1},
             {"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 2}]
    stub = WayfireStub(tmp_path / "wf.socket", outputs, views)
    try:
        for bad in (black, transparent):
            grim = _grim(tmp_path, {"DP-1": visible, "DP-2": bad})
            env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar), "PC_GRIM": str(grim)}
            assert subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".15"], env=env).returncode == 1
            for child in (tmp_path / "bin", tmp_path / "captures"):
                for item in child.iterdir(): item.unlink()
                child.rmdir()
    finally:
        stub.close()


def test_wrong_output_assignment_and_capture_failure_fail_closed(tmp_path):
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    visible = tmp_path / "visible.png"; _png(visible)
    outputs = [{"id": 1, "name": "DP-1"}, {"id": 2, "name": "DP-2"}]
    views = [{"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1},
             {"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1}]
    stub = WayfireStub(tmp_path / "wf.socket", outputs, views)
    grim = _grim(tmp_path, {"DP-1": visible, "DP-2": visible})
    env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar), "PC_GRIM": str(grim)}
    try:
        assert subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".15"], env=env).returncode == 1
        stub.views[1]["output-id"] = 2
        Path(grim).write_text("#!/bin/sh\nexit 1\n"); Path(grim).chmod(0o755)
        assert subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".15"], env=env).returncode == 1
    finally:
        stub.close()


def test_zero_damage_capture_timeout_never_declares_ready(tmp_path):
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    stub = WayfireStub(tmp_path / "wf.socket", [{"id": 1, "name": "DP-1"}],
                       [{"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1}])
    grim = tmp_path / "grim"; _fake(grim, "sleep 2\n")
    env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar),
                        "PC_GRIM": str(grim), "PC_GRIM_TIMEOUT": ".05"}
    try:
        done = subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".1"], env=env,
                              capture_output=True, timeout=5)
        assert done.returncode == 1
    finally:
        stub.close()


def test_shell_surfaces_always_receive_visual_health_marker_argument():
    main = (ROOT / "desktop/main.js").read_text()
    preload = (ROOT / "desktop/preload.js").read_text()
    assert "SHELL_MODE ? ['--pc-shell-health-marker']" in main
    assert "process.argv.includes('--pc-shell-health-marker')" in preload
    assert all("#%02x%02x%02x" % colour in preload for colour in MARKER)


def test_unreaped_gpu_process_is_not_mistaken_for_a_live_shell(tmp_path):
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    child = subprocess.Popen(["/bin/true"])
    time.sleep(.05)  # exited but deliberately not wait()ed: a real launcher-owned zombie
    views = [{"pid": child.pid, "app-id": "place.poster.desktop", "output-id": 1}]
    stub = WayfireStub(tmp_path / "wayfire.socket", [{"id": 1, "name": "DP-1"}], views)
    env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar)}
    started = time.monotonic()
    try:
        done = subprocess.run([str(HEALTH), "wait", str(child.pid), "2"], env=env,
                              text=True, capture_output=True)
        assert done.returncode == 1
        assert time.monotonic() - started < .8
    finally:
        child.wait(); stub.close()


def _fake(path, body):
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _launcher_env(tmp_path):
    runtime = tmp_path / "run"
    home = tmp_path / "home"
    runtime.mkdir(); home.mkdir()
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    launch = tmp_path / "posterchan"
    _fake(launch, f'echo launch >>"{tmp_path / "launches"}"\ntrap "exit 0" TERM INT HUP\nwhile :; do sleep .1; done\n')
    health = tmp_path / "health"
    _fake(health, 'if [ "$1" = preflight ]; then exit 0; fi\n'
                  '[ "${PC_TEST_FAIL_FIRST:-0}" = 1 ] && [ "$(wc -l <"$PC_TEST_LAUNCHES")" -eq 1 ] && exit 1\n'
                  'kill -0 "$2" 2>/dev/null || exit 1\n: >"$PC_TEST_HEALTHY"\nexit 0\n')
    # The production script prefers /usr/local; substitute only that resolved assignment in a test copy.
    script = tmp_path / "start"
    script.write_text(LAUNCHER.read_text().replace("launcher=/usr/local/bin/posterchan",
                                                    f"launcher={launch}"), encoding="utf-8")
    script.chmod(0o755)
    env = os.environ | {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime),
                        "WAYFIRE_SOCKET": str(runtime / "wf.socket"),
                        "PC_WAYFIRE_HEALTH": str(health), "PC_DESKTOP_ASAR": str(asar),
                        "PC_TEST_HEALTHY": str(tmp_path / "healthy"),
                        "PC_TEST_LAUNCHES": str(tmp_path / "launches")}
    return script, env


def test_simultaneous_wayfire_launchers_create_one_shell(tmp_path):
    script, env = _launcher_env(tmp_path)
    first = subprocess.Popen([str(script)], env=env)
    try:
        for _ in range(100):
            if (tmp_path / "healthy").exists(): break
            time.sleep(.02)
        second = subprocess.run([str(script)], env=env, timeout=2)
        assert second.returncode == 0
        assert (tmp_path / "launches").read_text().splitlines() == ["launch"]
    finally:
        first.terminate(); first.wait(timeout=3)


def test_package_replacement_after_mapping_fails_closed(tmp_path):
    script, env = _launcher_env(tmp_path)
    health = Path(env["PC_WAYFIRE_HEALTH"])
    _fake(health, 'if [ "$1" = preflight ]; then exit 0; fi\nprintf changed >>"$PC_DESKTOP_ASAR"\nexit 0\n')
    done = subprocess.run([str(script)], env=env, text=True, capture_output=True, timeout=5)
    assert done.returncode != 0
    assert "changed while the shell was starting" in done.stderr
    assert not Path(env["XDG_RUNTIME_DIR"], "posterchan-wayfire-ready").exists()


def test_gpu_early_death_gets_one_clean_cache_retry(tmp_path):
    script, env = _launcher_env(tmp_path)
    env["PC_TEST_FAIL_FIRST"] = "1"
    launches = tmp_path / "launches"
    launch = tmp_path / "posterchan"
    _fake(launch, f'echo launch >>"{launches}"\n[ "$(wc -l <"{launches}")" -eq 1 ] && exit 42\ntrap "exit 0" TERM INT HUP\nwhile :; do sleep .1; done\n')
    for name in ("GPUCache", "DawnCache", "GrShaderCache"):
        target = Path(env["HOME"], ".config/posterchan-desktop", name)
        target.mkdir(parents=True, exist_ok=True); (target / "stale").write_text("x")
    proc = subprocess.Popen([str(script)], env=env)
    try:
        for _ in range(150):
            if (tmp_path / "healthy").exists() and launches.exists() and len(launches.read_text().splitlines()) == 2:
                break
            time.sleep(.02)
        assert launches.read_text().splitlines() == ["launch", "launch"]
        assert all(not Path(env["HOME"], ".config/posterchan-desktop", name).exists()
                   for name in ("GPUCache", "DawnCache", "GrShaderCache"))
    finally:
        proc.terminate(); proc.wait(timeout=3)
