import json
import os
import socket
import struct
import subprocess
import threading
import time
import zlib
import re
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
SECONDARY_MARKER = [(75, 92, 255), (255, 92, 53), (155, 219, 77), (246, 213, 92)]


def _png(path, marker=True, transparent=False, colours=MARKER):
    width = height = 32
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            rgb = (0, 0, 0)
            if marker and 1 <= x < 9 and 1 <= y < 9:
                rgb = colours[(y >= 5) * 2 + (x >= 5)]
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
    visible = tmp_path / "visible.png"; secondary = tmp_path / "secondary.png"
    _png(visible); _png(secondary, colours=SECONDARY_MARKER)
    grim = _grim(tmp_path, {"DP-1": visible, "DP-2": secondary})
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
    _png(visible); _png(black, marker=False); _png(transparent, marker=True, transparent=True,
                                                   colours=SECONDARY_MARKER)
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
    visible = tmp_path / "visible.png"; secondary = tmp_path / "secondary.png"
    _png(visible); _png(secondary, colours=SECONDARY_MARKER)
    outputs = [{"id": 1, "name": "DP-1"}, {"id": 2, "name": "DP-2"}]
    views = [{"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1},
             {"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1}]
    stub = WayfireStub(tmp_path / "wf.socket", outputs, views)
    grim = _grim(tmp_path, {"DP-1": visible, "DP-2": secondary})
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
    assert all("#%02x%02x%02x" % colour in preload for colour in SECONDARY_MARKER)
    assert "MutationObserver" in preload
    assert "if(!document.getElementById('pc-shell-health-marker'))installHealthMarker()" in preload


def test_two_outputs_showing_the_same_shell_surface_is_still_a_failure(tmp_path):
    """THE DUPLICATE-SURFACE FAILURE, WHICH IS WHAT THE PER-OUTPUT COLOURS WERE REALLY FOR.

    This used to be spelled "the secondary output must carry the SECONDARY marker", which asked the
    probe to know which monitor the shell had chosen as primary -- and it guessed, because Wayfire's
    list-outputs has no `focused` field to read. See the companion test below for what that cost.
    One renderer painting both screens is the real fault and it is still caught here, by name.
    """
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    primary = tmp_path / "primary.png"; _png(primary)
    outputs = [{"id": 1, "name": "DP-1", "focused": True, "geometry": {"x": 0, "y": 0}},
               {"id": 2, "name": "DP-2", "geometry": {"x": 3840, "y": 0}}]
    # Reverse ids to prove marker roles come from display policy, not mapping timing.
    views = [{"id": 50, "pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1},
             {"id": 2, "pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 2}]
    stub = WayfireStub(tmp_path / "wf.socket", outputs, views)
    grim = _grim(tmp_path, {"DP-1": primary, "DP-2": primary})
    runtime = tmp_path / "runtime"; runtime.mkdir()
    env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar),
                        "PC_GRIM": str(grim), "XDG_RUNTIME_DIR": str(runtime)}
    try:
        done = subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".15"], env=env,
                              text=True, capture_output=True)
        assert done.returncode == 1
        assert "both show the primary shell surface" in done.stderr
    finally:
        stub.close()


def test_the_primary_surface_may_live_on_either_monitor(tmp_path):
    """AND THIS IS WHY THAT RULE HAD TO GO. Measured on the real two-monitor desktop: the shell put
    its PRIMARY surface on the RIGHT-hand output, while the probe demanded it on the top-left one
    (its fallback, since Wayfire reports no focused output). So `healthy()` was false on a desktop
    drawing perfectly on both screens -- every shell start after the first failed the gate, the
    launcher gave up, pc-compositor-session stopped Wayfire, and the login came back on Sway. That
    is the whole "the desktop keeps ending up on Sway" report, and nothing was wrong with it.
    """
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    primary = tmp_path / "primary.png"; _png(primary, colours=MARKER)
    secondary = tmp_path / "secondary.png"; _png(secondary, colours=SECONDARY_MARKER)
    outputs = [{"id": 1, "name": "DP-1", "geometry": {"x": 0, "y": 0}},
               {"id": 2, "name": "DP-2", "geometry": {"x": 3840, "y": 0}}]
    views = [{"id": 50, "pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1},
             {"id": 2, "pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 2}]
    stub = WayfireStub(tmp_path / "wf.socket", outputs, views)
    # The PRIMARY marker on the RIGHT output — the arrangement the real machine came up in.
    grim = _grim(tmp_path, {"DP-1": secondary, "DP-2": primary})
    runtime = tmp_path / "runtime"; runtime.mkdir()
    env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar),
                        "PC_GRIM": str(grim), "XDG_RUNTIME_DIR": str(runtime)}
    try:
        done = subprocess.run([str(HEALTH), "wait", str(os.getpid()), "2"], env=env,
                              text=True, capture_output=True)
        assert done.returncode == 0, done.stderr
    finally:
        stub.close()


def test_an_output_showing_no_shell_surface_at_all_is_a_failure(tmp_path):
    """The other half of the invariant: a monitor the desktop never painted."""
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    primary = tmp_path / "primary.png"; _png(primary, colours=MARKER)
    blank = tmp_path / "blank.png"; _png(blank, marker=False)
    outputs = [{"id": 1, "name": "DP-1", "geometry": {"x": 0, "y": 0}},
               {"id": 2, "name": "DP-2", "geometry": {"x": 3840, "y": 0}}]
    views = [{"id": 50, "pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1},
             {"id": 2, "pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 2}]
    stub = WayfireStub(tmp_path / "wf.socket", outputs, views)
    grim = _grim(tmp_path, {"DP-1": primary, "DP-2": blank})
    runtime = tmp_path / "runtime"; runtime.mkdir()
    env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar),
                        "PC_GRIM": str(grim), "XDG_RUNTIME_DIR": str(runtime)}
    try:
        done = subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".15"], env=env,
                              text=True, capture_output=True)
        assert done.returncode == 1
        assert "DP-2 view=2 no shell marker" in done.stderr
        assert (runtime / "posterchan-health-DP-2.png").exists()
    finally:
        stub.close()


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


#: Bound sockets are closed when garbage collected, which would delete the path out from under the
#: launcher mid-run; hold a reference for the life of the test session.
_KEEP_ALIVE: list = []


def _launcher_env(tmp_path):
    runtime = tmp_path / "run"
    home = tmp_path / "home"
    runtime.mkdir(); home.mkdir()
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    launch = tmp_path / "posterchan"
    _fake(launch, f'echo launch >>"{tmp_path / "launches"}"\necho "$DISPLAY" >>"{tmp_path / "displays"}"\ntrap "exit 0" TERM INT HUP\nwhile :; do sleep .1; done\n')
    health = tmp_path / "health"
    _fake(health, 'if [ "$1" = preflight ]; then exit 0; fi\n'
                  '[ "${PC_TEST_FAIL_FIRST:-0}" = 1 ] && [ "$(wc -l <"$PC_TEST_LAUNCHES")" -eq 1 ] && exit 1\n'
                  'kill -0 "$2" 2>/dev/null || exit 1\n: >"$PC_TEST_HEALTHY"\nexit 0\n')
    # The production script prefers /usr/local; substitute only that resolved assignment in a test copy.
    script = tmp_path / "start"
    script.write_text(LAUNCHER.read_text().replace("launcher=/usr/local/bin/posterchan",
                                                    f"launcher={launch}"), encoding="utf-8")
    script.chmod(0o755)
    # A WAYFIRE SOCKET IS NOT A WAYLAND DISPLAY. The launcher checks for both, and refuses rather
    # than launching Electron into "Failed to connect to Wayland display" -- so the harness has to
    # provide a real one, exactly as the compositor does.
    display = socket.socket(socket.AF_UNIX)
    display.bind(str(runtime / "wayland-9"))
    _KEEP_ALIVE.append(display)
    env = os.environ | {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime),
                        "DISPLAY": ":99", "WAYLAND_DISPLAY": "wayland-9",
                        "WAYFIRE_SOCKET": str(runtime / "wf.socket"),
                        "PC_WAYFIRE_HEALTH": str(health), "PC_DESKTOP_ASAR": str(asar),
                        "PC_TEST_HEALTHY": str(tmp_path / "healthy"),
                        "PC_TEST_LAUNCHES": str(tmp_path / "launches")}
    return script, env


def test_wayfire_launcher_xwayland_wait_is_bounded_and_testable():
    source = LAUNCHER.read_text()
    assert 'if [ -z "${DISPLAY:-}" ]' in source
    assert 'PC_XWAYLAND_SOCKET:-/tmp/.X11-unix/X0' in source
    assert 'PC_XWAYLAND_WAIT_TENTHS:-100' in source
    assert 'export DISPLAY' in source


def test_wayfire_launcher_waits_for_xwayland_socket_and_exports_display(tmp_path):
    script, env = _launcher_env(tmp_path)
    env.pop("DISPLAY", None)
    xsocket = tmp_path / "X0"
    env["PC_XWAYLAND_SOCKET"] = str(xsocket)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(xsocket))
    proc = subprocess.Popen([str(script)], env=env)
    try:
        for _ in range(100):
            if (tmp_path / "healthy").exists(): break
            time.sleep(.02)
        assert (tmp_path / "displays").read_text().splitlines() == [":0"]
    finally:
        proc.terminate(); proc.wait(timeout=3); server.close()


def test_wayfire_launcher_xwayland_wait_times_out_without_blocking_launch(tmp_path):
    script, env = _launcher_env(tmp_path)
    env.pop("DISPLAY", None)
    env["PC_XWAYLAND_SOCKET"] = str(tmp_path / "missing-X0")
    env["PC_XWAYLAND_WAIT_TENTHS"] = "1"
    proc = subprocess.Popen([str(script)], env=env)
    try:
        for _ in range(100):
            if (tmp_path / "healthy").exists(): break
            time.sleep(.02)
        assert (tmp_path / "displays").read_text().splitlines() == [":0"]
    finally:
        proc.terminate(); proc.wait(timeout=3)


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


def test_wayfire_launcher_does_not_mistake_health_marker_renderer_for_shell():
    source = LAUNCHER.read_text()
    assert "grep -Fx -- '--shell'" in source
    assert "posterchan-desktop .*--shell" not in source
    assert "--pc-shell-health-marker" in source


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


def test_the_marker_is_only_painted_on_a_shell_surface():
    """A POPPED-OUT WINDOW IS THE SAME PROCESS, AND process.argv IS THE PROCESS'S.

    Every PosterChan window and every popup is a same-origin child in the shell's renderer, so all
    of them saw `--pc-shell-health-marker` and painted the 8x8 diagnostic square in their own
    top-left corner — permanently, 1px from the corner of the TITLE BAR rather than of the screen.
    Reported as "why do all the posterchan windows have a colored square on the left? that is ugly".

    The watchdog contract is unchanged: the probe screenshots the two SHELL surfaces, and those
    still paint it.
    """
    src = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
    gate = src.split("if(process.argv.includes('--pc-shell-health-marker')", 1)[1].split("\n", 1)[0]
    assert "_pcShellSurface()" in gate, gate
    fn = src.split("const _pcShellSurface", 1)[1].split("\n};", 1)[0]
    # A popped-out window is opened through window.open and keeps a live opener; a shell surface is
    # created by main.js and has none.
    assert "window.opener" in fn
    # And the menus, which main.js loads WITHOUT an opener but with a ?pcpopup= query.
    assert "pcpopup" in fn
    # It must fail CLOSED on a throw: a cross-origin opener read raises, and a window that cannot
    # answer the question is not a shell surface.
    assert "catch(_){ return false; }" in fn


def test_the_probe_still_looks_for_both_shell_markers():
    """The two shell surfaces are told apart by colour, and the gate reads either."""
    probe = (ROOT / "os/bin/pc-wayfire-health").read_text(encoding="utf-8")
    assert "PRIMARY_MARKER" in probe and "SECONDARY_MARKER" in probe
    preload = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
    assert "--pc-secondary-surface" in preload


def test_the_marker_is_retired_once_the_shell_is_declared_ready():
    """IT IS READ ONCE, AND IT USED TO STAY FOR EVER.

    `pc-wayfire-health wait` screenshots the outputs and looks for the marker before the launcher
    declares the desktop ready. After that nothing ever reads it again — but it sat in the corner of
    the screen for the life of the session, on a machine whose entire job is to look like a desktop:
    "there is a color box on the top left of the desktop too".

    The verdict already has a name in the filesystem — the launcher writes `$PC_WAYFIRE_READY_FILE`
    immediately after the probe passes — so the retirement is driven by the real signal rather than
    by a guess about how long startup takes.
    """
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    fn = main.split("function armHealthMarkerRetirement(", 1)[1].split("\n}\n", 1)[0]
    assert "PC_WAYFIRE_READY_FILE" in fn, fn
    assert "pc:host:health-marker-off" in fn
    # Only the shell surfaces paint it, so only they are armed.
    assert "if (SHELL_MODE) armHealthMarkerRetirement(created);" in main
    # A session with no launcher (started by hand, another compositor) still loses the square, and
    # the cap is longer than the launcher's own worst case so it cannot retire one the probe needs:
    # 10s for the Xwayland socket plus a 30s health gate.
    cap = int(re.search(r"const cap = setTimeout\(.*?,\s*(\d+)\)", fn, re.S).group(1))
    assert cap >= 60000, cap

    # THE MESSAGE CAN ARRIVE BEFORE THE LISTENER EXISTS. The ready file appears while the renderer
    # is still loading, and an ipc send with no listener is dropped — measured on the machine: the
    # log said "health marker retired" four times and the square was still on screen. Both sides
    # cover the race: the sender repeats on every `did-finish-load`, and the renderer announces
    # itself so a late listener is told about a retirement that already happened.
    assert "target.webContents.on('did-finish-load', tell)" in fn, fn
    assert "pc:host:health-marker-listening" in main
    preload = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
    assert "ipcRenderer.on('pc:host:health-marker-off', dropHealthMarker)" in preload
    assert "ipcRenderer.send('pc:host:health-marker-listening')" in preload
    drop = preload.split("const dropHealthMarker", 1)[1].split("\n  };", 1)[0]
    # The MutationObserver re-installs the marker whenever the client replaces the document, so
    # retiring it means stopping that too — otherwise it is put straight back.
    assert "disconnect()" in drop
    assert "m.remove()" in drop


def _wlr(tmp_path, text, name="wlr-randr"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tool = tmp_path / name
    _fake(tool, "cat <<'EOF'\n" + text + "\nEOF\n")
    return tool


def test_a_blanked_screen_is_not_a_desktop_that_failed_to_draw(tmp_path):
    """grim cannot copy a DPMS-off output, and reading that as "it did not render" is what left a
    laptop with a live compositor and NO desktop after an unattended update.

    Measured: a shell restart while the panel had blanked logged "no shell marker" on every retry
    and the launcher gave up with "failed the Wayfire surface/GPU health gate". The machine had
    blanked because nobody was at it -- which is precisely why the update was left running.
    """
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    outputs = [{"id": 1, "name": "eDP-1"}]
    views = [{"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1}]
    stub = WayfireStub(tmp_path / "wf.socket", outputs, views)
    grim = tmp_path / "grim"
    _fake(grim, 'echo "failed to copy output eDP-1" >&2\nexit 1\n')
    off = _wlr(tmp_path, 'eDP-1 "Lenovo (eDP-1)"\n  Make: Lenovo\n  Enabled: no\n  Modes:\n    1920x1080 px')
    on = _wlr(tmp_path, name="wlr-randr-on", text='eDP-1 "Lenovo (eDP-1)"\n  Make: Lenovo\n  Enabled: yes\n  Modes:\n    1920x1080 px')
    base = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar),
                         "PC_GRIM": str(grim), "PC_GRIM_TIMEOUT": ".5"}
    try:
        blanked = subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".3"], text=True,
                                 capture_output=True, env=base | {"PC_WLR_RANDR": str(off)})
        assert blanked.returncode == 0, blanked.stderr
        assert "powered off" in blanked.stderr, blanked.stderr

        # AND THE CHECK STILL FAILS FOR THE REASON IT EXISTS. The same unreadable capture on an
        # output the compositor says is POWERED is a desktop that did not render.
        powered = subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".3"], text=True,
                                 capture_output=True, env=base | {"PC_WLR_RANDR": str(on)})
        assert powered.returncode == 1, powered.stderr
        assert "no shell marker" in powered.stderr

        # With no wlr-randr at all the answer is "I do not know", which keeps the strict behaviour.
        unknown = subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".3"], text=True,
                                 capture_output=True,
                                 env=base | {"PC_WLR_RANDR": str(tmp_path / "no-such-tool")})
        assert unknown.returncode == 1, unknown.stderr
    finally:
        stub.close()


def test_a_second_screen_being_off_does_not_hide_a_broken_first_one(tmp_path):
    """Skipping a blanked output must not turn the multi-monitor checks off with it."""
    asar = tmp_path / "app.asar"; asar.write_bytes(b"wm-wayfire.js")
    black = tmp_path / "black.png"; _png(black, marker=False)
    outputs = [{"id": 1, "name": "DP-1"}, {"id": 2, "name": "DP-2"}]
    views = [{"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 1},
             {"pid": os.getpid(), "app-id": "place.poster.desktop", "output-id": 2}]
    stub = WayfireStub(tmp_path / "wf.socket", outputs, views)
    # DP-1 captures and is black; DP-2 cannot be captured and is switched off.
    grim = tmp_path / "grim"
    _fake(grim, f'if [ "$2" = "DP-1" ]; then cp "{black}" "$3"; else exit 1; fi\n')
    off = _wlr(tmp_path, 'DP-1 "a"\n  Enabled: yes\nDP-2 "b"\n  Enabled: no')
    env = os.environ | {"WAYFIRE_SOCKET": stub.path, "PC_DESKTOP_ASAR": str(asar),
                        "PC_GRIM": str(grim), "PC_WLR_RANDR": str(off), "PC_GRIM_TIMEOUT": ".5"}
    try:
        done = subprocess.run([str(HEALTH), "wait", str(os.getpid()), ".3"], text=True,
                              capture_output=True, env=env)
        assert done.returncode == 1, done.stderr
        assert "no shell marker" in done.stderr
    finally:
        stub.close()
