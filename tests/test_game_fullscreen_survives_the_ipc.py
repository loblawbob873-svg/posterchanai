"""A GAME IS FULLSCREENED BY EXACTLY ONE THING, AND ITS ONLY TRIGGER COULD GO DEAF FOR EVER.

Reported as "i tried a game and cursor went to other monitor", hours after Gamescope became opt-in
and games started launching directly again.

WHAT WAS MEASURED ON THE REAL TWO-MONITOR DESK (Wayfire 0.10.1, DP-1 + DP-2, scale 1.25), with an
XWayland window whose WM_CLASS is `steam_app_999999` — the same string Proton publishes and the same
string every promotion path here keys on:

  * `wm-actions/set-fullscreen` WORKS when it is called: by hand against a `foot` window,
    `{'result': 'ok'}` and the view is 0,0 3072x2048 a second later.
  * The shell never called it in time. Run one: still floating after 9s. Run two: promoted at
    8.25s (its own ConfigureNotify says so). The design budget is 180ms/900ms/2500ms.

WHY. `window-rules/events/watch` is registered PER IPC CONNECTION and `WayfireWM` holds ONE socket
for requests and events alike. When it closed — `Error: Wayfire IPC closed`, eight times in one
session's shell.log — the next `_send` transparently opened a NEW socket that had never asked to
watch anything, while `this.subscribed` stayed true so nothing ever asked again. Every window event
stopped for the life of the process, with the compositor healthy, every request still answering, and
nothing in any log. `enforceNativeGameFullscreen` is the ONLY thing that fullscreens a Steam title on
this session (the compositor rule that claimed to was a no-op; Gamescope, which used to map its
surface already fullscreen, is now opt-in), and it is driven exclusively by those events.

A latch set around an attempt whose subject can go away — the shape this codebase has been bitten by
before, and the reason the first test below RUNS the shipped backend against a real socket rather
than reading it.

AND FULLSCREEN IS NOT CONFINEMENT. Also measured: Wayfire advertises `zwp_pointer_constraints_v1`
and `zwp_relative_pointer_manager_v1`, and Xwayland carries the client half — so a game that grabs is
held, and a game sitting in a menu is not, on any wlroots compositor. `force-fullscreen` is the only
plugin on this machine with a pointer option at all, so it is offered as a key.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
WAYFIRE = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(
    encoding="utf-8")

# A fake Wayfire IPC server, speaking the real uint32-le JSON framing, that answers the watch
# request, then DROPS the connection exactly the way the live socket did. It reports every method
# it was asked for, on every connection, so the test can see whether the watch was re-armed on the
# second one — which is the whole question.
HARNESS = r"""
const net=require('net'), fs=require('fs'), os=require('os'), path=require('path');
const {WayfireWM}=require(process.argv[1]);
const sock=path.join(fs.mkdtempSync(path.join(os.tmpdir(),'wfipc-')),'wayfire-wayland-9-.socket');
const seen=[];           // [connection index, method]
let conns=0, live=null;
function frame(v){const b=Buffer.from(JSON.stringify(v));const h=Buffer.alloc(4);h.writeUInt32LE(b.length);return Buffer.concat([h,b]);}
const server=net.createServer(c=>{
  const n=++conns; live=c;
  let buf=Buffer.alloc(0);
  c.on('data',ch=>{
    buf=buf.length?Buffer.concat([buf,ch]):ch;
    for(;;){
      if(buf.length<4)return;
      const len=buf.readUInt32LE(0);
      if(buf.length<4+len)return;
      const msg=JSON.parse(buf.subarray(4,4+len).toString());
      buf=buf.subarray(4+len);
      seen.push([n,msg.method]);
      c.write(frame({result:'ok'}));
    }
  });
  c.on('error',()=>{});
});
server.listen(sock, async () => {
  const wm=new WayfireWM(sock);
  const events=[];
  wm.on('window', ev => events.push(String(ev&&ev.change||'')));
  let reconnects=0;
  wm.on('reconnect', () => reconnects++);
  await wm.subscribe();
  // The live socket closed under the shell eight times in one session. Do exactly that.
  live.destroy();
  await new Promise(r=>setTimeout(r,2500));
  // An event delivered on whatever connection the backend now holds.
  if(live && !live.destroyed) live.write(frame({event:'view-mapped',view:{id:7,'app-id':'steam_app_999999'}}));
  await new Promise(r=>setTimeout(r,250));
  console.log(JSON.stringify({
    watchesPerConnection: seen.filter(x=>x[1]==='window-rules/events/watch').map(x=>x[0]),
    connections: conns, events, reconnects, subscribed: wm.subscribed}));
  process.exit(0);
});
"""


def _run_harness() -> dict:
    # Its own XDG_RUNTIME_DIR: the backend opens `posterchan-action.sock` there and unlinks any
    # socket already at that path — never do that to the runtime dir of the machine running tests.
    import tempfile
    with tempfile.TemporaryDirectory() as runtime:
        env = dict(os.environ, XDG_RUNTIME_DIR=runtime)
        env.pop("WAYFIRE_SOCKET", None)
        proc = subprocess.run(
            ["node", "-e", HARNESS, str(ROOT / "desktop/wm-wayfire.js")],
            capture_output=True, text=True, timeout=90, cwd=str(ROOT), env=env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class WatchSurvivesAReconnect(unittest.TestCase):
    """RUNS the shipped Wayfire backend. A structural test cannot see this: every line of the old
    `subscribe()` was correct, and only the fact that a watch dies with its connection was not."""

    def test_the_watch_is_re_armed_on_the_new_connection(self):
        got = _run_harness()
        self.assertGreaterEqual(got["connections"], 2,
                                "the backend never reconnected after the socket closed")
        self.assertIn(1, got["watchesPerConnection"], "the first watch was never sent")
        self.assertTrue([c for c in got["watchesPerConnection"] if c > 1],
                        "the shell reconnected and never asked to watch anything again — every "
                        "window event is lost for the life of the process, and with it the only "
                        f"thing that fullscreens a game: {got}")

    def test_an_event_after_the_reconnect_still_reaches_a_listener(self):
        got = _run_harness()
        self.assertIn("view-mapped", got["events"],
                      f"a view mapped after the reconnect reached nobody: {got}")

    def test_the_reconnect_is_announced_so_missed_events_can_be_swept_for(self):
        got = _run_harness()
        self.assertGreaterEqual(got["reconnects"], 1)
        self.assertTrue(got["subscribed"], "the backend still believes it is deaf")


class ThePromotionHasAPathThatNoEventIsNeededFor(unittest.TestCase):
    def test_the_sweep_is_armed_by_a_launch_not_only_by_an_event(self):
        """Every bounded sweep was scheduled FROM a window event, so a lost event cancelled the
        feature outright and nothing else could notice."""
        self.assertIn("function armNativeGameSweep(ms){", MAIN)
        launch = MAIN[MAIN.index("ipcMain.handle('pc:wm:launch'"):]
        launch = launch[:launch.index("/* EVERY APP INSTALLED ON THIS MACHINE")]
        self.assertIn("armNativeGameSweep(120000)", launch,
                      "a game launch no longer arms the promotion sweep")
        self.assertIn("opts.game||opts.gamescope", launch.replace(" ", ""))

    def test_the_sweep_is_bounded_and_asks_once_per_view(self):
        """Unbounded, this is a fullscreen watchdog: it would drag somebody back into fullscreen
        every two seconds after they deliberately left it."""
        fn = MAIN[MAIN.index("function armNativeGameSweep(ms){"):]
        fn = fn[:fn.index("function scheduleNativeGameReconcile(){")]
        self.assertIn("_nativeGameSweepUntil", fn)
        self.assertIn("clearInterval(_nativeGameSweepTimer)", fn)
        self.assertIn("_nativeGameFullscreenAsked.has(id)", MAIN)

    def test_a_reconnect_sweeps_for_what_it_missed(self):
        self.assertIn("wm().on('reconnect', () => armNativeGameSweep(30000));", MAIN)


class TheDesktopIdlesBehindAGame(unittest.TestCase):
    """58.8% of a core at idle, 113.0% while a client went fullscreen over DP-1, 80.2% steady
    behind it — measured in 5s /proc windows across wayfire and every shell process. Busier, not
    quieter, beside the game and beside OBS's encoder."""

    def test_main_measures_occlusion_per_output_and_says_so(self):
        self.assertIn("function publishShellOcclusion(rows){", MAIN)
        fn = MAIN[MAIN.index("function publishShellOcclusion(rows){"):]
        fn = fn[:fn.index("/* A PROMOTION THAT ONLY EVENTS")]
        self.assertIn("row.fullscreen", fn)
        self.assertIn("scope.output", fn, "occlusion is not scoped to an output, so a game on one "
                                          "monitor would stop the desktop on the other")
        self.assertIn("name:'occlusion'", fn.replace(" ", ""))
        self.assertIn("place\\.poster\\.desktop", fn,
                      "our own full-output shell surface would report itself occluded for ever")

    def test_the_renderer_stops_its_timers_and_starts_them_again(self):
        self.assertIn("function _setDesktopIdle(idle){", OS_JS)
        fn = OS_JS[OS_JS.index("function _setDesktopIdle(idle){"):]
        fn = fn[:fn.index("\n  }") + 4]
        for stopped in ("_wgtStop()", "clearInterval(_natBeat)", "clearInterval(_clock)"):
            self.assertIn(stopped, fn, f"{stopped} still runs behind a fullscreen game")
        self.assertIn("_wgtRefreshDue(true)", fn,
                      "coming back must CATCH UP; a widget whose interval fell due behind the game "
                      "would otherwise show a stale reading until its next period")
        self.assertIn("_startClock()", fn)
        self.assertIn("if(ev.name === 'occlusion')", OS_JS)

    def test_a_stopped_ticker_is_not_restarted_by_its_own_guards(self):
        """`_wgtStart` is called from several places; without the check the very next call undoes
        this, which is indistinguishable from the feature not existing."""
        start = OS_JS[OS_JS.index("function _wgtStart(){"):]
        start = start[:start.index("function _wgtStop()")]
        self.assertIn("if(_deskIdle) return;", start)
        self.assertIn("if(_deskIdle){ _wgtStop(); return; }", start)
        self.assertIn("if(_natBeat || _deskIdle || !window.pcWM) return;", OS_JS)

    def test_the_idle_class_disables_animation_and_never_pauses_it(self):
        """A paused animation holds whatever keyframe it reached — that is how `anim-off` once
        froze a whole timeline at opacity:0. A disabled one resolves to the resting style."""
        rule = CSS[CSS.index("html.os-idle #os-root"):]
        rule = rule[:rule.index("}") + 1]
        self.assertIn("animation:none!important", rule)
        self.assertNotIn("animation-play-state", rule)


class NothingConfinesAPointerByAccident(unittest.TestCase):
    def test_the_one_pointer_option_this_compositor_has_is_actually_configured(self):
        """Fullscreen is not confinement on any wlroots compositor. `force-fullscreen` is the only
        plugin whose metadata mentions `constrain` at all."""
        plugins = next(l for l in WAYFIRE.splitlines() if l.startswith("plugins = "))
        self.assertIn("force-fullscreen", plugins,
                      "the constraint options below configure a plugin that is never loaded")
        block = WAYFIRE[WAYFIRE.index("[force-fullscreen]"):]
        self.assertIn("constrain_pointer = true", block)
        self.assertIn("constraint_area = output", block,
                      "constraining to the VIEW is not what 'cursor went to other monitor' asks for")

    def test_the_steam_client_is_still_not_wrapped_in_a_nested_compositor(self):
        """Every per-game .desktop entry on the machine is `Exec=steam steam://rungameid/<id>`,
        which hands the URL to the running client and exits — so wrapping a game launch reaches the
        game only by wrapping STEAM, which is the thing that made every window Deck-shaped."""
        block = MAIN[MAIN.index("if(opts&&opts.gamescope&&process.env.WAYFIRE_SOCKET)"):]
        block = block[:block.index("/* TELEGRAM")]
        self.assertNotIn("--force-windows-fullscreen", block)
        self.assertNotIn("opts&&opts.game&&process.env.WAYFIRE_SOCKET", MAIN)


class TheMachineHasAHardwareVideoEncoder(unittest.TestCase):
    """MEASURED on the real box: `/usr/lib64/dri/*_drv_video.so` is EMPTY — Mesa was built with
    `-vaapi`, so there is no VA driver at all — while the kernel advertises the card's encode rings
    (`ring vcn_enc_0.0 uses VM inv eng 1`). Nothing reports that: libva answers "unknown libva
    error" and every caller silently falls back to the CPU. OBS's profile on that machine is
    `Encoder=obs_x264` at 3840x2560@60, i.e. a software H.264 encode of a 9.8-megapixel screencast
    on the same CPU as the game, and the desktop shell logs `vaInitialize failed` for the same
    reason. This does not touch anybody's OBS settings — it makes the encoder exist."""

    def test_mesa_is_built_with_vaapi(self):
        src = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")
        line = next(l for l in src.splitlines()
                    if l.startswith("SPECIAL_PACKAGE_USE") and "media-libs/mesa" in l)
        mesa = line.split("media-libs/mesa", 1)[1].split('"', 1)[0]
        self.assertIn("vaapi", mesa,
                      "Mesa builds no VA driver, so this machine has no hardware video encoder or "
                      "decoder however capable the card is: " + mesa.strip())
        self.assertNotIn("-vaapi", mesa)


if __name__ == "__main__":
    unittest.main()
