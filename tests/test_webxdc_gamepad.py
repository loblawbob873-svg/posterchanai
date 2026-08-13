"""The webxdc gamepad shim — run the SHIPPED bridge code under node against a stub pad.

Run: venv-unified/bin/python -m unittest tests.test_webxdc_gamepad

WHY THIS EXISTS AS A TEST AT ALL. The thing being asserted is a translation between two APIs neither
of which is present here: a Bluetooth controller and a game. Every part of it fails SILENTLY — a
synthetic KeyboardEvent with a 0 keyCode dispatches perfectly and moves nothing in an emscripten
build; an event aimed at `document` never passes through <body>; a stick resting near one threshold
taps a key sixty times a second instead of holding it. All of those look, from outside, exactly like
"the controller does not work", which is the report this shim was written to answer in the first
place. So the real code is driven with real gamepad snapshots and the emitted key stream is read
back, rather than the source being grepped for the mapping table.

The rules asserted here are decisions, not accidents:

  * A direction emits BOTH the arrow and the WASD key (Doom reads one, Quake the other).
  * keyCode is carried, because that is the field emscripten/SDL actually reads.
  * A held button emits ONE keydown, not one per frame.
  * The stick has hysteresis, so a stick resting near the edge holds rather than chatters.
  * An app that calls navigator.getGamepads() does NOT turn the shim off. It used to, and that one
    assumption made the whole feature do nothing — SDL2's emscripten joystick backend polls the pad
    by default while the game reads only the keyboard. Polling is reported; the off switch is real
    and belongs to the client.
  * The loop does not wait for `gamepadconnected`, which may never reach a doubly-nested cross-origin
    frame.
  * It reports what it measured, because the failure happens on a phone that isn't here.
  * Backgrounding releases whatever was held, rather than leaving the player walking into a wall.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBXDC_JS = ROOT / "static" / "js" / "client" / "webxdc.js"

# The shim is one self-contained IIFE inside the BRIDGE template string. Extracted rather than
# re-declared here, so this drives the code that actually ships; if it is renamed or deleted, the
# extraction fails loudly instead of the suite passing on an absent feature.
START = "/* A GAME CONTROLLER, FOR APPS THAT ONLY SPEAK KEYBOARD."
END = "  send({ jsonrpc:'2.0', method:'webxdc.hello' });"


def shim_source() -> str:
    src = WEBXDC_JS.read_text()
    i = src.find(START)
    if i < 0:
        raise AssertionError(
            "the gamepad shim's marker comment is gone from webxdc.js — if it was renamed, "
            "update START here; if it was deleted, that is the bug this test exists for"
        )
    j = src.find(END, i)
    if j < 0:
        raise AssertionError("could not find the end of the bridge after the gamepad shim")
    body = src[i:j]
    k = body.find("(function(){")
    if k < 0:
        raise AssertionError("the gamepad shim is no longer an IIFE")
    return body[k:].rstrip().rstrip(";")


# A stub browser: one controllable frame clock, one dispatch target that records, and a gamepad
# snapshot the test rewrites between frames. `hidden`/`activeElement`/`canvas` are real fields
# because the shim branches on all three.
HARNESS = r"""
const events = [];
const padstats = [];
let pads = [];
// The bridge globals the shim now uses: `__XDC` carries the client-owned off switch, and `send` is
// the postMessage channel it reports the diagnostic on.
global.__XDC = { pad: PAD_ENABLED };
// CLONED, because postMessage structured-clones and this stub does not. Holding the live `stat`
// object made every recorded sample read as the LAST one — the test equivalent of the reference bug
// the music panel hit, and it would have hidden exactly the transitions this is here to check.
global.send = (m) => { if(m && m.method === 'webxdc.padstat') padstats.push(JSON.parse(JSON.stringify(m.params))); };
// Bridge-scope state the shim reads: a controller Android handed in. Declared here because the test
// extracts the shim IIFE alone, while in the real bridge these live one scope up.
global.padNative = null;
global.padAt = 0;
let statFn = null;
global.setInterval = (fn) => { statFn = fn; return 0; };
let pending = null;
let appCalls = 0;

const canvas = { __name:'canvas', dispatchEvent(e){ events.push({ on:'canvas', type:e.type,
                 key:e.key, code:e.code, keyCode:e.keyCode, which:e.which,
                 bubbles:e.bubbles }); return true; } };
const bodyEl = { __name:'body', dispatchEvent(e){ events.push({ on:'body', type:e.type,
                 key:e.key, code:e.code, keyCode:e.keyCode, bubbles:e.bubbles }); return true; } };

global.KeyboardEvent = class KeyboardEvent {
  constructor(type, init){
    init = init || {};
    this.type = type;
    this.key = init.key; this.code = init.code;
    this.bubbles = init.bubbles; this.cancelable = init.cancelable;
    // Deliberately HONOURED here, the way Chrome does. The shim's defineProperty fallback is for
    // engines that drop them; asserting the happy path is what proves the dict is being filled in.
    this.keyCode = init.keyCode || 0;
    this.which = init.which || 0;
  }
};
global.document = {
  hidden: false,
  activeElement: null,
  body: bodyEl,
  documentElement: { __name:'html' },
  querySelector(sel){ return sel === 'canvas' ? canvas : null; },
};
const listeners = {};
global.window = { addEventListener(t, fn){ (listeners[t] = listeners[t] || []).push(fn); } };
// `navigator` is a read-only built-in global from node 21 on, so a plain assignment silently does
// nothing and the shim sees the REAL navigator (no getGamepads), returns early, and every assertion
// here fails for a reason that has nothing to do with the shim.
Object.defineProperty(global, 'navigator', {
  value: { getGamepads(){ appCalls++; return pads; } }, writable: true, configurable: true });
global.requestAnimationFrame = (fn) => { pending = fn; return 1; };

// The shim wraps navigator.getGamepads at load, so grab the wrapper afterwards to play "the app".
SHIM;

const appGetGamepads = navigator.getGamepads.bind(navigator);

function emit(type){ (listeners[type] || []).forEach(fn => fn()); }
function frame(){ const fn = pending; pending = null; if(fn) fn(); }
function drain(){ events.length = 0; }

// ---- the script the python side drives -------------------------------------------------------
const out = {};
const steps = STEPS;
for(const s of steps){
  if(s.pads !== undefined) pads = s.pads.map(p => p && ({ connected:true, mapping:'standard',
      buttons:(p.buttons || []).map(v => ({ pressed: v > 0.5, value:v })), axes:p.axes || [] }));
  if(s.hidden !== undefined) document.hidden = s.hidden;
  if(s.native !== undefined){ padNative = s.native; padAt = s.native ? Date.now() : 0; }
  if(s.nativeStale){ padAt = Date.now() - 5000; }
  if(s.focus !== undefined) document.activeElement = s.focus === 'body' ? bodyEl : null;
  if(s.emit) emit(s.emit);
  if(s.appPolls) appGetGamepads();
  if(s.drain) drain();
  if(s.frames) for(let i = 0; i < s.frames; i++) frame();
  if(s.record) out[s.record] = events.map(e => ({ on:e.on, type:e.type, code:e.code,
                                                  keyCode:e.keyCode, bubbles:e.bubbles }));
  // The report rides a timer in the browser; fire it by hand so the test controls when.
  if(s.recordStat){ if(statFn) statFn();
    out[s.recordStat] = padstats.length ? padstats[padstats.length-1] : null; }
}
console.log(JSON.stringify(out));
"""


def run(steps, pad_enabled=True):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = (HARNESS.replace("SHIM;", shim_source() + ";")
              .replace("STEPS", json.dumps(steps))
              .replace("PAD_ENABLED", "true" if pad_enabled else "false"))
    p = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError("node failed: " + (p.stderr or "")[-3000:])
    return json.loads(p.stdout)


def pad(buttons=None, axes=None):
    b = [0.0] * 16
    for i in buttons or []:
        b[i] = 1.0
    return {"buttons": b, "axes": axes or [0.0, 0.0]}


DPAD_UP, DPAD_DOWN, DPAD_LEFT, A_BTN = 12, 13, 14, 0


class WebxdcGamepadShim(unittest.TestCase):
    def codes(self, evs, type_):
        return [e["code"] for e in evs if e["type"] == type_]

    def test_dpad_emits_both_the_arrow_and_the_wasd_key(self):
        """Doom reads the arrows and Quake reads WASD; a shim that picks one is dead in half the
        gallery. Nothing here is a chord, so an app that knows only one never sees the other."""
        out = run([
            {"pads": [pad()], "emit": "gamepadconnected", "frames": 1, "drain": True},
            {"pads": [pad(buttons=[DPAD_UP])], "frames": 1, "record": "down"},
        ])
        self.assertEqual(sorted(self.codes(out["down"], "keydown")), ["ArrowUp", "KeyW"])

    def test_keycode_is_carried_because_that_is_what_emscripten_reads(self):
        """The silent one. An event with key/code set and keyCode 0 dispatches perfectly and moves
        nothing in an SDL build, which is indistinguishable from the shim not running at all."""
        out = run([
            {"pads": [pad()], "emit": "gamepadconnected", "frames": 1, "drain": True},
            {"pads": [pad(buttons=[DPAD_UP])], "frames": 1, "record": "down"},
        ])
        by_code = {e["code"]: e["keyCode"] for e in out["down"]}
        self.assertEqual(by_code["ArrowUp"], 38)
        self.assertEqual(by_code["KeyW"], 87)

    def test_a_held_button_emits_one_keydown_not_one_per_frame(self):
        """A key repeated sixty times a second is a game that fires continuously and a menu that
        scrolls past everything — the classic way a polled shim reads as broken."""
        out = run([
            {"pads": [pad()], "emit": "gamepadconnected", "frames": 1, "drain": True},
            {"pads": [pad(buttons=[A_BTN])], "frames": 10, "record": "held"},
        ])
        self.assertEqual(self.codes(out["held"], "keydown"), ["Space"])
        self.assertEqual(self.codes(out["held"], "keyup"), [])

    def test_release_emits_keyup(self):
        out = run([
            {"pads": [pad()], "emit": "gamepadconnected", "frames": 1},
            {"pads": [pad(buttons=[A_BTN])], "frames": 2, "drain": True},
            {"pads": [pad()], "frames": 2, "record": "up"},
        ])
        self.assertEqual(self.codes(out["up"], "keyup"), ["Space"])

    def test_the_stick_has_hysteresis_so_a_resting_stick_holds_instead_of_chattering(self):
        """A single threshold and a stick sitting near it gives keyup+keydown pairs at frame rate,
        which a game reads as tapping rather than holding. Harder to leave than to enter: 0.5 in,
        0.35 out."""
        out = run([
            {"pads": [pad()], "emit": "gamepadconnected", "frames": 1, "drain": True},
            {"pads": [pad(axes=[-0.6, 0.0])], "frames": 1, "record": "enter"},
            # Back inside the entry threshold but outside the exit one: still held, nothing emitted.
            {"pads": [pad(axes=[-0.42, 0.0])], "frames": 5, "drain": True},
            {"pads": [pad(axes=[-0.42, 0.0])], "frames": 5, "record": "linger"},
            {"pads": [pad(axes=[-0.20, 0.0])], "frames": 1, "record": "leave"},
        ])
        self.assertEqual(sorted(self.codes(out["enter"], "keydown")), ["ArrowLeft", "KeyA"])
        self.assertEqual(out["linger"], [])
        self.assertEqual(sorted(self.codes(out["leave"], "keyup")), ["ArrowLeft", "KeyA"])

    def test_an_app_polling_gamepads_does_NOT_turn_the_shim_off(self):
        """THE BUG THAT MADE THE FIRST VERSION DO NOTHING, kept as a test so it cannot come back.

        The shim used to disable itself the moment the app touched navigator.getGamepads(), reasoning
        that an app reading the pad does not need fake keys. But SDL2's emscripten joystick backend
        is compiled in BY DEFAULT and emscripten_sample_gamepad_data() IS navigator.getGamepads() —
        so any port that inits the joystick subsystem (SDL_INIT_EVERYTHING is enough) polls every
        frame while its input code reads only the keyboard. That is precisely the game this shim
        exists for, and it switched itself off on the first frame. Polling is reported now, never
        acted on."""
        out = run([
            {"pads": [pad()], "frames": 1, "drain": True},
            {"appPolls": True},
            {"pads": [pad(buttons=[DPAD_UP])], "frames": 2, "record": "after", "recordStat": "stat"},
        ])
        self.assertEqual(sorted(self.codes(out["after"], "keydown")), ["ArrowUp", "KeyW"])
        self.assertGreater(out["stat"]["appPolls"], 0, "the app's polling should still be REPORTED")

    def test_the_client_can_switch_the_shim_off(self):
        """The real off switch, for an app that genuinely reads the pad — owned by the client, since
        the app frame has no settings UI of its own."""
        out = run([
            {"pads": [pad(buttons=[DPAD_UP])], "frames": 3, "record": "keys"},
        ], pad_enabled=False)
        self.assertEqual(out["keys"], [])

    def test_it_runs_without_ever_seeing_gamepadconnected(self):
        """The second way this could do nothing. Whether `gamepadconnected` reaches a doubly-nested
        cross-origin frame is exactly what nobody could confirm, so the loop no longer waits for it —
        note that no step here emits the event."""
        out = run([
            {"pads": [pad(buttons=[A_BTN])], "frames": 2, "record": "keys"},
        ])
        self.assertEqual(self.codes(out["keys"], "keydown"), ["Space"])

    def test_it_reports_what_it_measured(self):
        """There is no controller and no phone on the machine this is written on, so the difference
        between "no pad", "pad but no press" and "keys sent, game ignored them" has to come back
        from the device or the next round is another guess."""
        out = run([
            {"pads": [], "frames": 2, "recordStat": "empty"},
            {"pads": [pad(buttons=[DPAD_LEFT])], "frames": 2, "recordStat": "pressed"},
        ])
        self.assertEqual(out["empty"]["pads"], 0)
        self.assertEqual(out["empty"]["presses"], 0)
        self.assertEqual(out["pressed"]["pads"], 1)
        self.assertGreater(out["pressed"]["presses"], 0)
        self.assertIn("left", out["pressed"]["last"])

    def test_backgrounding_releases_what_was_held(self):
        """Otherwise the player is still walking into a wall when they come back to the phone."""
        out = run([
            {"pads": [pad()], "emit": "gamepadconnected", "frames": 1},
            {"pads": [pad(buttons=[DPAD_LEFT])], "frames": 2, "drain": True},
            {"hidden": True, "frames": 1, "record": "bg"},
        ])
        self.assertEqual(sorted(self.codes(out["bg"], "keyup")), ["ArrowLeft", "KeyA"])

    def test_the_event_targets_the_canvas_and_bubbles(self):
        """An event dispatched on `document` never passes through <body>, and one on `window` passes
        through nothing — aiming at the wrong node is a shim that fires and reaches no listener. The
        deepest plausible node plus bubbles serves canvas, body, document and window at once."""
        out = run([
            {"pads": [pad()], "emit": "gamepadconnected", "frames": 1, "drain": True},
            {"pads": [pad(buttons=[A_BTN])], "frames": 1, "record": "ev"},
        ])
        self.assertEqual([e["on"] for e in out["ev"]], ["canvas"])
        self.assertTrue(all(e["bubbles"] for e in out["ev"]))

    def test_no_pad_no_keys(self):
        out = run([
            {"pads": [], "emit": "gamepadconnected", "frames": 5, "record": "none"},
        ])
        self.assertEqual(out["none"], [])

    def test_the_shim_is_still_in_the_shipped_bridge(self):
        """The extraction above is the real assertion; this names it so a deletion reads as a failed
        feature rather than a confusing parse error."""
        src = shim_source()
        self.assertIn("gamepadconnected", src)
        self.assertIn("navigator.getGamepads", src)
        # It has to be inside the BRIDGE string: on the app's origin is the only place it can reach
        # the app's document. In the client's own scope it would be dispatching into nothing.
        whole = WEBXDC_JS.read_text()
        bridge = whole[whole.index("const BRIDGE = `"):whole.index("})();`")]
        self.assertIn(START, bridge)


    def test_a_controller_handed_in_by_android_drives_the_game(self):
        """THE APK CASE. The same game, pad and tablet works in Firefox and does nothing in the app —
        one variable, the engine. Rather than keep trying to settle whether WebView implements the
        Gamepad API, Android reads the controller and hands a Gamepad-shaped snapshot in. No pad is
        visible to navigator.getGamepads() in this test at all, which is exactly the APK's situation."""
        out = run([
            {"pads": [], "native": {"buttons": [0]*12 + [1, 0, 0, 0], "axes": [0, 0, 0, 0]},
             "frames": 2, "record": "keys", "recordStat": "stat"},
        ])
        self.assertEqual(sorted(self.codes(out["keys"], "keydown")), ["ArrowUp", "KeyW"])
        self.assertEqual(out["stat"]["src"], "native")

    def test_a_stale_native_pad_is_not_believed(self):
        """A bridge that stops reporting must not pin the player to the last thing it said — holding
        'up' for ever is worse than losing the controller, because it is indistinguishable from the
        game being stuck."""
        out = run([
            {"pads": [], "native": {"buttons": [0]*12 + [1, 0, 0, 0], "axes": [0, 0, 0, 0]},
             "frames": 2, "drain": True},
            {"nativeStale": True, "frames": 2, "record": "after", "recordStat": "stat"},
        ])
        self.assertEqual(sorted(self.codes(out["after"], "keyup")), ["ArrowUp", "KeyW"])
        self.assertEqual(out["stat"]["src"], "none")

    def test_a_real_gamepad_still_works_when_no_native_one_is_handed_in(self):
        """Firefox and the desktop already work; the native path must not regress them."""
        out = run([
            {"pads": [pad(buttons=[A_BTN])], "native": None, "frames": 2,
             "record": "keys", "recordStat": "stat"},
        ])
        self.assertEqual(self.codes(out["keys"], "keydown"), ["Space"])
        self.assertEqual(out["stat"]["src"], "webapi")

if __name__ == "__main__":
    unittest.main()
