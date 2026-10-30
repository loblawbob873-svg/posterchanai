"""pc-window-effects: wobbly windows and the open/close/minimize effect on PosterChanOS.

"we need the window effects on posterchan os, wobbly windows, exploding windows, etc. its own settings menu"
(2026-10-29). The SHIPPED helper is run against a fake Wayfire IPC socket that records every call, the way the
running compositor answers it (measured on the laptop: core/plugins and the animate options read back, and
set-config-options answering {"result":"ok"}). What it must get right:

  * the person's choice is stored in their own home directory and pushed into the running compositor;
  * wobbly / extra-animations are added to the plugin list only when chosen, removed when not, and every OTHER
    plugin stays exactly where it was (a list rebuilt from a default would drop posterchan-shell);
  * the desktop surface itself is excluded from every effect, PosterChan's own app windows are not;
  * a name the machine does not offer is never sent (the default stands instead);
  * with nothing chosen, it sets what Wayfire already does (zoom / squeezimize, no wobbly);
  * no compositor = saved, said, nothing thrown.
"""
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "os/bin/pc-window-effects"
OVERLAY = ROOT / "os/overlay/app-misc/posterchanos-shell"
RUNNING = ("animate autostart command decoration fast-switcher force-fullscreen foreign-toplevel grid idle ipc "
           "ipc-rules posterchan-shell move place resize view-shot vswitch window-rules wm-actions")


class FakeWayfire:
    """A Unix socket that speaks Wayfire's IPC framing: u32 length + JSON, both ways."""

    def __init__(self, path):
        self.path, self.calls, self.opts = str(path), [], {"core/plugins": RUNNING, "animate/open_animation": "zoom",
                                                           "animate/close_animation": "zoom", "animate/minimize_animation": "squeezimize"}
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(self.path)
        self.srv.listen(8)
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            n = struct.unpack("<I", c.recv(4))[0]
            buf = b""
            while len(buf) < n:
                buf += c.recv(n - len(buf))
            msg = json.loads(buf)
            self.calls.append(msg)
            if msg["method"] == "wayfire/get-config-option":
                out = {"result": "ok", "value": self.opts.get(msg["data"]["option"], "")}
            elif msg["method"] == "wayfire/set-config-options":
                self.opts.update(msg["data"])
                out = {"result": "ok"}
            else:
                out = {"error": "no such method"}
            body = json.dumps(out).encode()
            c.sendall(struct.pack("<I", len(body)) + body)
            c.close()

    def sets(self):
        return [m["data"] for m in self.calls if m["method"] == "wayfire/set-config-options"]


def metadata(tmp_path, extra=True, wobbly=True):
    d = tmp_path / "metadata"
    d.mkdir(exist_ok=True)
    for name, on in (("animate", True), ("extra-animations", extra), ("wobbly", wobbly)):
        if on:
            (d / (name + ".xml")).write_text("<wayfire/>")
    return d


def run(tmp_path, *args, socket_path=None, meta=None):
    env = {**os.environ, "PC_WINDOW_EFFECTS_CONF": str(tmp_path / "home/window-effects.json"),
           "PC_WAYFIRE_METADATA": str(meta or metadata(tmp_path)), "XDG_RUNTIME_DIR": str(tmp_path / "run"),
           "WAYFIRE_SOCKET": str(socket_path or "")}
    (tmp_path / "run").mkdir(exist_ok=True)
    r = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, env=env, timeout=20)
    return r


def test_choosing_wobbly_and_explode_loads_both_and_keeps_every_other_plugin(tmp_path):
    wf = FakeWayfire(tmp_path / "wf.sock")
    r = run(tmp_path, "set", json.dumps({"open": "fire", "close": "shatter", "wobbly": True, "wobbliness": "high", "speed": "slow"}),
            socket_path=wf.path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert not out.get("error"), out
    sent = wf.sets()[-1]
    plugins = sent["core/plugins"].split()
    assert plugins[:len(RUNNING.split())] == RUNNING.split(), ("another plugin was moved or dropped", plugins)
    assert "wobbly" in plugins and "extra-animations" in plugins, plugins
    assert sent["animate/open_animation"] == "fire" and sent["animate/close_animation"] == "shatter", sent
    assert sent["extra-animations/shatter_duration"] == "1750ms linear", ("Slow is 1.75x the effect's own length", sent)
    assert sent["wobbly/spring_k"] < 8.0 and sent["wobbly/friction"] < 3.0, ("Jelly wobbles more than the default", sent)
    assert json.loads((tmp_path / "home/window-effects.json").read_text())["close"] == "shatter", "the choice was not stored"
    assert out["chosen"]["close"] == "shatter" and out["running"]["close"] == "shatter", out

    # and back to nothing chosen: wobbly and extra-animations leave, the rest stays, effects are Wayfire's own
    r = run(tmp_path, "set", "{}", socket_path=wf.path)
    sent = wf.sets()[-1]
    assert sent["core/plugins"] == RUNNING, ("turning them off must leave the list as it was", sent["core/plugins"])
    assert (sent["animate/open_animation"], sent["animate/close_animation"], sent["animate/minimize_animation"]) == ("zoom", "zoom", "squeezimize")


def test_the_desktop_itself_is_never_animated_but_posterchan_windows_are(tmp_path):
    wf = FakeWayfire(tmp_path / "wf.sock")
    run(tmp_path, "set", json.dumps({"close": "shatter"}), socket_path=wf.path)
    rule = wf.sets()[-1]["animate/enabled_for"]
    # the shell surfaces' app_ids, as `window-rules/list-views` reported them on the laptop
    for app_id in ("place.poster.desktop", "posterchan-desktop", "PosterChan"):
        assert 'app_id is "%s"' % app_id in rule, rule
    assert '!(title contains "PosterChan Window")' in rule, ("the app windows share the app_id and must stay animated", rule)
    assert rule.startswith('(type equals "toplevel"'), rule


def test_a_name_this_machine_does_not_offer_is_never_sent(tmp_path):
    wf = FakeWayfire(tmp_path / "wf.sock")
    meta = metadata(tmp_path, extra=False, wobbly=False)
    r = run(tmp_path, "set", json.dumps({"open": "rm -rf", "close": "shatter", "wobbly": True, "speed": "ludicrous"}),
            socket_path=wf.path, meta=meta)
    out = json.loads(r.stdout)
    sent = wf.sets()[-1]
    assert sent["animate/open_animation"] == "zoom", sent
    assert sent["animate/close_animation"] == "zoom", ("shatter without wayfire-plugins-extra", sent)
    assert "wobbly" not in sent.get("core/plugins", RUNNING).split(), sent
    assert "shatter" not in out["offered"]["close"] and out["offered"]["wobbly"] is False, out
    assert out["chosen"]["speed"] == "normal", out


def test_apply_pushes_the_stored_choice_and_no_compositor_is_said_not_thrown(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "home/window-effects.json").write_text(json.dumps({"close": "fire", "wobbly": True}))
    wf = FakeWayfire(tmp_path / "wf.sock")
    assert run(tmp_path, "apply", socket_path=wf.path).returncode == 0
    sent = wf.sets()[-1]
    assert sent["animate/close_animation"] == "fire" and "wobbly" in sent["core/plugins"].split(), sent

    r = run(tmp_path, "set", json.dumps({"close": "melt"}), socket_path=str(tmp_path / "nothing.sock"))
    out = json.loads(r.stdout)
    assert r.returncode == 0 and "next sign-in" in out["error"], out
    assert json.loads((tmp_path / "home/window-effects.json").read_text())["close"] == "melt", "saved even with no compositor"


def test_it_is_installed_and_applied_at_sign_in():
    assert HELPER.read_bytes() == (OVERLAY / "files/pc-window-effects").read_bytes(), "os/bin and the overlay copy have drifted"
    assert os.access(HELPER, os.X_OK), "the helper must be executable"
    ebuild = next(OVERLAY.glob("posterchanos-shell-*.ebuild")).read_text()
    assert " pc-window-effects " in ebuild, "the package has to install the helper"
    assert " pc-window-effects " in (ROOT / "os/gentoo.sh").read_text(), "a fresh install copies the helpers itself"
    ini = (OVERLAY / "files/wayfire.ini").read_text()
    assert "\nwindow_effects = /usr/local/bin/pc-window-effects apply\n" in ini, "[autostart] must re-apply the choice"
    assert "gui-libs/wayfire-plugins-extra" in (ROOT / "os/gentoo.sh").read_text(), "shatter/vortex/melt come from it"


def test_fire_colour_and_rainbow_reach_the_compositor_and_nothing_else_does(tmp_path):
    """'add more effects like the cube and fire colors' (2026-10-29). Burn's colour is the person's (measured on the
    laptop: a cyan fire_color turns the closing window's flames cyan), or every flame its own (random_fire_color)."""
    wf = FakeWayfire(tmp_path / "wf.sock")
    run(tmp_path, "set", json.dumps({"close": "fire", "fire_color": "#00F0FF"}), socket_path=wf.path)
    sent = wf.sets()[-1]
    assert sent["animate/fire_color"] == "#00f0ffff" and sent["animate/random_fire_color"] is False, sent
    run(tmp_path, "set", json.dumps({"close": "fire", "fire_color": "red; rm -rf ~", "fire_rainbow": True}), socket_path=wf.path)
    sent = wf.sets()[-1]
    assert sent["animate/fire_color"] == "#b22303ff", ("a value that is not a colour must fall back to the default", sent)
    assert sent["animate/random_fire_color"] is True, sent
