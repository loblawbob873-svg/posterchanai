import json
import os
import socket
import struct
import subprocess
import threading
import time
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


def test_health_requires_exactly_one_owned_surface_per_output(tmp_path):
    asar = tmp_path / "app.asar"
    asar.write_bytes(b'wm-wayfire.js')
    outputs = [{"id": 1}, {"id": 2}]
    views = [{"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1},
             {"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 2}]
    stub = WayfireStub(tmp_path / "wayfire.socket", outputs, views)
    env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar)}
    try:
        assert subprocess.run([str(HEALTH), "preflight"], env=env).returncode == 0
        assert subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".3"], env=env).returncode == 0
        stub.views.append(dict(views[0]))
        failed = subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".2"], env=env,
                                text=True, capture_output=True)
        assert failed.returncode == 1
        assert "exactly one shell surface per output" in failed.stderr
    finally:
        stub.close()


def test_unreaped_gpu_process_is_not_mistaken_for_a_live_shell(tmp_path):
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    child = subprocess.Popen(["/bin/true"])
    time.sleep(.05)  # exited but deliberately not wait()ed: a real launcher-owned zombie
    views = [{"pid": child.pid, "app-id": "place.poster.desktop", "output-id": 1}]
    stub = WayfireStub(tmp_path / "wayfire.socket", [{"id": 1}], views)
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
