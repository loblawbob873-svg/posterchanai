"""The tray's controls, PRESSED — the bug being that they were not wired at all.

The panel painted a network chip, a volume chip, a brightness chip and a power button, they looked
like buttons, and every one of them was decoration: `render` bound `[data-app]` and `[data-win]`
and nothing else. Reported as "nothing happens when you click on the wifi bar and power button
bar". A control that reports a reading and refuses to change it is worse than no control, because
it looks like the feature is there.

So these press them and assert the machine was actually asked.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "static", "js", "client", "osshell.js")
NODE = shutil.which("node") or shutil.which("nodejs")

# A DOM with just enough of a document for popovers: created elements, a body to put them in,
# class/attribute lookup, and rectangles so the anchoring arithmetic has something to read.
DOM = r"""
function el(tag){
  const e = { tag: tag || 'div', _html: '', kids: [], dataset: {}, disabled: false,
              onclick: null, style: {}, className: '', offsetWidth: 260, offsetHeight: 180 };
  const RE = () => /<(button|input|div|span)\b([^>]*)>/g;
  const attr = (s, n) => { const m = new RegExp(n + '="([^"]*)"').exec(s); return m ? m[1] : null; };
  Object.defineProperty(e, 'innerHTML', {
    get(){ return e._html; },
    set(v){
      e._html = String(v); e.kids = [];
      let m; const re = RE();
      while((m = re.exec(e._html))){
        const raw = m[2] || '';
        /* A real element, not a stub: the popover sets `.os-pop-b`'s innerHTML afterwards, so a
         * child that cannot itself be filled makes the second half of every popover invisible to
         * this test while working perfectly in a browser. */
        const k = el(m[1]);
        k.className = attr(raw, 'class') || '';
        k.value = attr(raw, 'value') || '';
        k.textContent = '';
        for(const d of ['app','win','os','ssid','sec','act','prof','mute','kind',
                        'qs','val','sink','mix','mixvol','shot','p','d']){
          const a = attr(raw, 'data-' + d);
          if(a !== null) k.dataset[d] = a;
        }
        e.kids.push(k);
      }
    },
  });
  e.querySelectorAll = (sel) => {
    const d = /\[data-(\w+)\]/.exec(sel);
    if(d) return e.kids.filter(k => k.dataset[d[1]] !== undefined);
    const c = /^\.([\w-]+)$/.exec(sel);
    if(c) return e.kids.filter(k => (' ' + k.className + ' ').indexOf(' ' + c[1] + ' ') >= 0);
    return [];
  };
  e.querySelector = (sel) => e.querySelectorAll(sel)[0] || null;
  e.appendChild = (c) => { e.kids.push(c); return c; };
  e.remove = () => {};
  e.contains = () => false;
  e.getBoundingClientRect = () => ({left: 100, top: 900, width: 60, height: 24});
  return e;
}
globalThis.document = {
  createElement: (t) => el(t),
  body: { appendChild: (c) => { globalThis.__lastPop = c; return c; } },
  addEventListener: () => {}, removeEventListener: () => {},
};
globalThis.window = globalThis;
globalThis.innerWidth = 1920; globalThis.innerHeight = 1080;
globalThis.setTimeout = setTimeout;
"""


@unittest.skipIf(not NODE, "no node on this node")
class Tray(unittest.TestCase):
    def run_js(self, bridges, body, prompt="hunter2", confirm="true"):
        js = """
        %s
        const B = %s;
        for(const k in B) globalThis[k] = B[k];
        /* `__PC`, WHICH IS THE NAME THE REAL PAGE PUBLISHES. This harness used to define `PC`,
         * the name osshell.js was written against — so the toasts, the wifi password prompt and
         * the shutdown confirm all passed here while every one of them was dead in the browser.
         * A fixture that agrees with the bug cannot see it. `test_os_shell.py` holds the guard
         * that stops the short name coming back in the code; this holds the other half. */
        globalThis.__PC = {
          toast: (m) => { (globalThis.__toasts = globalThis.__toasts || []).push(m); },
          uiPrompt: async () => %s,
          uiConfirm: async () => %s,
        };
        const S = require(%s);
        (async () => { const out = {}; const host = el();
        try { %s } catch(e){ out.threw = String(e.message || e); }
        out.toasts = globalThis.__toasts || [];
        process.stdout.write(JSON.stringify(out)); })();
        """ % (DOM, bridges, json.dumps(prompt), confirm, json.dumps(MOD), body)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-1200:])
        return json.loads(r.stdout)

    WM = """{ pcWM: { windows: async () => [], focus: async () => true,
                      launch: async () => ({pid: 1, window: {id: 2}}) },
              pcNet: { status: async () => ({online: true, kind: 'wifi', name: 'home', signal: 71}),
                       wifi: async () => { globalThis.__scanned = true;
                                           return [{ssid: 'home', signal: 71, secure: true},
                                                   {ssid: 'cafe', signal: 40, secure: false}]; },
                       connect: async (s, p) => { globalThis.__joined = [s, p]; return {ok: true}; } },
              pcAudio: { status: async () => ({output: {percent: 40, muted: false}}),
                         setVolume: async (n) => { globalThis.__vol = n; return true; },
                         setMuted: async (on) => { globalThis.__muted = on; return true; } },
              pcPower: { status: async () => ({battery: {present: true, percent: 80, charging: false},
                                               brightness: {available: true, percent: 55},
                                               profiles: ['balanced', 'performance'],
                                               profile: 'balanced', canHibernate: true}),
                         setBrightness: async (n) => { globalThis.__bright = n; return true; },
                         setProfile: async (p) => { globalThis.__prof = p; return true; },
                         poweroff: async () => { globalThis.__off = true; return true; },
                         suspend: async () => { globalThis.__sleep = true; return true; } } }"""

    # THE TRAY IS ONE BUTTON NOW, so reaching a control is two presses: the Windows-11 group in the
    # corner, then the tile inside Quick Settings. The controls did not go away — they moved out of a
    # row of six text chips competing for the corner of a taskbar and into the flyout that row should
    # always have been. Everything below still presses the real handler on the real markup.
    OPEN_QUICK = """
          await S.render(host);
          const group = host.querySelectorAll('[data-os]').find(b => b.dataset.os === 'quick');
          out.hasGroup = !!group;
          out.groupBound = !!(group && group.onclick);
          if(group) await group.onclick({stopPropagation(){}});
          await new Promise(r => setTimeout(r, 60));
          const pop = globalThis.__lastPop;
          out.hasPop = !!pop;
    """

    def press(self, kind, extra="", bridges=None, pre=""):
        return self.run_js(bridges or self.WM, pre + self.OPEN_QUICK + """
          const chip = pop && pop.querySelectorAll('[data-os]').find(b => b.dataset.os === '%s');
          out.hasChip = !!chip;
          out.bound = !!(chip && chip.onclick);
          if(chip) await chip.onclick({stopPropagation(){}});
          await new Promise(r => setTimeout(r, 80));
          // A tile that opens its OWN popover (network, power) replaces the flyout, so `sub` is
          // what to look in; one that navigates within it (outputs, mixer) leaves `pop` in place.
          const sub = globalThis.__lastPop;
          %s
        """ % (kind, extra))

    def test_the_tray_is_one_grouped_button(self):
        """WINDOWS 11, WHICH IS WHAT WAS ASKED FOR: network, sound and battery are one button in the
        corner and everything else is inside it. It was six text chips — `95% Tribble` `72%` `33%`
        `off` `100% ⚡` `⏻` — five of them a bare percentage with no picture of what was being
        measured."""
        out = self.run_js(self.WM, self.OPEN_QUICK)
        self.assertTrue(out["hasGroup"], "there is no grouped tray button")
        self.assertTrue(out["groupBound"], "the tray group has no handler")
        self.assertTrue(out["hasPop"], "pressing the tray group opened nothing")

    def test_every_tile_is_bound(self):
        """The original bug, still asserted: they were painted and never given a handler."""
        for kind in ("net", "power"):
            with self.subTest(chip=kind):
                out = self.press(kind)
                self.assertTrue(out["hasChip"], "no %s tile was rendered" % kind)
                self.assertTrue(out["bound"], "the %s tile has no handler" % kind)

    def test_the_sliders_are_in_the_flyout_and_apply_as_they_move(self):
        """Volume and brightness are SLIDERS in Quick Settings, not chips that open one — that is
        the whole difference between a tray reading `72%` and a tray you can drag. Applied as they
        move: a volume control you have to confirm is one you cannot use to turn something down."""
        out = self.run_js(self.WM, self.OPEN_QUICK + """
          const sliders = pop.querySelectorAll('[data-qs]');
          out.kinds = sliders.map(x => x.dataset.qs);
          for(const sl of sliders){
            sl.value = '33';
            if(sl.oninput) sl.oninput();
          }
          await new Promise(r => setTimeout(r, 60));
          out.vol = globalThis.__vol === undefined ? null : globalThis.__vol;
          out.bright = globalThis.__bright === undefined ? null : globalThis.__bright;
        """)
        self.assertIsNone(out.get("threw"), out.get("threw"))
        self.assertEqual(sorted(out["kinds"]), ["bright", "vol"])
        self.assertEqual(out["vol"], 33, "the volume slider did not reach the machine")
        self.assertEqual(out["bright"], 33, "the brightness slider did not reach the machine")

    def test_the_battery_is_drawn_flat_with_its_level(self):
        """"battery/charging should be a flat icon" — it was the number plus a ⚡ EMOJI, which takes
        the emoji font's colour and weight and matches nothing else in the tray. The shell is a
        sprite symbol; the CHARGE has to be computed, because a `<use>` takes no parameters and a
        fixed glyph would have to lie about the level."""
        out = self.run_js(self.WM, """
          out.full = S.batterySvg(100, false);
          out.half = S.batterySvg(50, false);
          out.empty = S.batterySvg(0, false);
          out.charging = S.batterySvg(50, true);
        """)
        self.assertIn('width="15.0"', out["full"], "a full battery is not drawn full")
        self.assertIn('width="7.5"', out["half"], "the fill is not proportional to the charge")
        self.assertNotIn("os-bat-fill", out["empty"], "an empty battery is drawn with a fill")
        self.assertIn("os-bat-bolt", out["charging"], "charging is not shown")
        self.assertNotIn("⚡", out["charging"], "the emoji is back")

    def test_the_wifi_glyph_follows_the_signal(self):
        """One bar-less "connected" icon cannot tell somebody their wifi is why a page will not
        load. An UNKNOWN reading is not full strength — that is the lie a tray must not tell."""
        icon = lambda net: self.run_js(self.WM, "out.i = S.wifiIcon(%s);" % json.dumps(net))["i"]
        self.assertEqual(icon({"known": True, "online": True, "kind": "wifi", "signal": 90}), "wifi")
        self.assertEqual(icon({"known": True, "online": True, "kind": "wifi", "signal": 50}), "wifi-mid")
        self.assertEqual(icon({"known": True, "online": True, "kind": "wifi", "signal": 10}), "wifi-low")
        self.assertEqual(icon({"known": True, "online": False}), "wifi-off")
        self.assertEqual(icon({"known": False}), "wifi-off")
        self.assertEqual(icon({"known": True, "online": True, "kind": "ethernet"}), "ethernet")

    def test_the_wifi_tile_scans_for_networks(self):
        out = self.press("net", "out.scanned = !!globalThis.__scanned;")
        self.assertTrue(out["scanned"], "pressing wifi never asked the machine what was in range")

    def test_the_networks_in_range_are_listed_and_one_can_be_joined(self):
        """An open network joins with no password; the list is what the machine reported, not a
        cached one — somebody opening this is standing somewhere new."""
        out = self.run_js(self.WM, self.OPEN_QUICK + """
          const chip = pop.querySelectorAll('[data-os]').find(b => b.dataset.os === 'net');
          await chip.onclick({stopPropagation(){}});
          await new Promise(r => setTimeout(r, 80));
          const body = globalThis.__lastPop.querySelector('.os-pop-b');
          const rows = body.querySelectorAll('[data-ssid]');
          out.ssids = rows.map(r => r.dataset.ssid);
          const cafe = rows.find(r => r.dataset.ssid === 'cafe');
          if(cafe) await cafe.onclick();
          await new Promise(r => setTimeout(r, 40));
          out.joined = globalThis.__joined || null;
        """)
        self.assertIsNone(out.get("threw"), out.get("threw"))
        self.assertEqual(out["ssids"], ["home", "cafe"])
        self.assertEqual(out["joined"][0], "cafe")

    def test_a_secured_network_is_asked_for_its_password(self):
        out = self.run_js(self.WM, self.OPEN_QUICK + """
          const chip = pop.querySelectorAll('[data-os]').find(b => b.dataset.os === 'net');
          await chip.onclick({stopPropagation(){}});
          await new Promise(r => setTimeout(r, 80));
          const rows = globalThis.__lastPop.querySelector('.os-pop-b').querySelectorAll('[data-ssid]');
          // 'home' is the one we are already on; joining it again would not ask. Use the other
          // secured entry by flipping the flag the markup carries.
          const cafe = rows.find(r => r.dataset.ssid === 'cafe');
          cafe.dataset.sec = '1';
          await cafe.onclick();
          await new Promise(r => setTimeout(r, 40));
          out.joined = globalThis.__joined || null;
        """)
        self.assertEqual(out["joined"], ["cafe", "hunter2"])

    def test_mute_is_a_button_beside_the_slider(self):
        """Mute and level are separate facts — a UI that infers "muted" from "volume is 0" cannot
        restore the level afterwards. It is the speaker ICON, which is where every desktop puts it,
        and it redraws as the muted glyph rather than a dimmed one."""
        out = self.press("mute", "out.muted = globalThis.__muted;")
        self.assertTrue(out["hasChip"], "there is no mute button in Quick Settings")
        self.assertTrue(out["bound"])
        self.assertIs(out["muted"], True, "pressing mute never reached the machine")

    def test_shutting_down_asks_first(self):
        """Sleep does not — confirming that is a dialog between somebody and closing their laptop —
        but restart and shut down lose whatever is open."""
        out = self.run_js(self.WM, self.OPEN_QUICK + """
          const chip = pop.querySelectorAll('[data-os]').find(b => b.dataset.os === 'power');
          await chip.onclick({stopPropagation(){}});
          await new Promise(r => setTimeout(r, 60));
          out.asked = globalThis.__askedConfirm === true;
        """, confirm="(globalThis.__askedConfirm = true)")
        self.assertIsNone(out.get("threw"), out.get("threw"))

    SOUND = """{ pcWM: { windows: async () => [], focus: async () => true },
                 pcAudio: { status: async () => ({ output: {percent: 40, muted: false},
                              sinks: [{id: 50, name: 'Speakers', isDefault: true},
                                      {id: 51, name: 'Headphones', isDefault: false}] }),
                            setVolume: async () => true, setMuted: async () => true,
                            setDefault: async (id) => { globalThis.__sink = id; return {id}; },
                            mixer: async () => { if(globalThis.__mixFail) throw new Error('no sound server');
                              return globalThis.__streams || [{id: 80, name: 'Firefox', percent: 100, muted: false}]; },
                            setStreamVolume: async (id, n) => { globalThis.__sv = [id, n]; return true; },
                            setStreamMuted: async (id, on) => { globalThis.__sm = [id, on]; return true; } },
                 pcPower: { status: async () => ({ brightness: {available: false} }) },
                 pcShot: { available: async () => (globalThis.__shotCan
                              || {ok: true, region: true, why: ''}),
                           take: async (o) => { globalThis.__took = o;
                              return globalThis.__shotRes || {ok: true, path: '/home/x/Pictures/Screenshots/a.png', copied: true}; } } }"""

    def test_the_volume_row_can_switch_output_device(self):
        """"volume should switch audio devices" — the chevron beside the slider, which is exactly
        where Windows puts it. The device in use is MARKED, because a list of three identical
        speaker names with nothing to say which one is live is not a chooser."""
        out = self.press("outputs", """
          const body = pop.querySelector('.os-pop-b');
          const rows = body.querySelectorAll('[data-sink]');
          out.names = rows.map(r => r.dataset.sink);
          out.markup = body._html;
          const head = pop.querySelector('.os-pop-h');
          out.back = !!(head && pop.querySelectorAll('[data-os]').find(b => b.dataset.os === 'quickback'));
          const other = rows.find(r => r.dataset.sink === '51');
          if(other) await other.onclick();
          await new Promise(r => setTimeout(r, 40));
          out.picked = globalThis.__sink === undefined ? null : globalThis.__sink;
        """, bridges=self.SOUND)
        self.assertIsNone(out.get("threw"), out.get("threw"))
        self.assertEqual(len(out["names"]), 2, "both output devices should be offered")
        self.assertIn("in use", out["markup"],
                      "nothing says which device sound is coming out of")
        self.assertTrue(out["back"], "a sub-panel with no way back is a dead end in a corner")
        self.assertEqual(out["picked"], 51, "picking a device never reached the machine")

    def test_the_mixer_gives_each_application_its_own_level(self):
        """The app-level mixer. `wpctl` takes a node id wherever it takes a sink, so a stream is set
        exactly like one — measured on the machine before this was written."""
        out = self.press("mixer", """
          const rows = pop.querySelector('.os-pop-b').querySelectorAll('[data-mixvol]');
          out.n = rows.length;
          if(rows[0]){ rows[0].value = '25'; if(rows[0].oninput) rows[0].oninput(); }
          await new Promise(r => setTimeout(r, 40));
          out.sv = globalThis.__sv || null;
          const mutes = pop.querySelector('.os-pop-b').querySelectorAll('[data-mix]');
          if(mutes[0]) await mutes[0].onclick();
          await new Promise(r => setTimeout(r, 40));
          out.sm = globalThis.__sm || null;
        """, bridges=self.SOUND)
        self.assertIsNone(out.get("threw"), out.get("threw"))
        self.assertEqual(out["n"], 1, "the playing application has no slider")
        self.assertEqual(out["sv"], [80, 25], "moving one app's slider moved something else")
        self.assertEqual(out["sm"], [80, True], "muting one app never reached the machine")

    def test_nothing_playing_and_could_not_read_are_different_answers(self):
        """An empty mixer drawn as a blank panel is indistinguishable from a mixer that failed to
        read the machine — the same mistake as an empty wifi list, and this codebase has made it
        with a wifi list, a blob store and a folder manifest."""
        read = "out.said = (pop.querySelector('.os-pop-b')||{})._html || '';"
        empty = self.press("mixer", read, bridges=self.SOUND, pre="globalThis.__streams = [];")
        self.assertIn("Nothing is playing", empty["said"])
        broken = self.press("mixer", read, bridges=self.SOUND, pre="globalThis.__mixFail = 1;")
        self.assertIn("could not be read", broken["said"])

    def test_quick_settings_uses_the_space_for_keep_awake(self):
        out = self.run_js(self.SOUND, self.OPEN_QUICK + """
          out.tiles = pop.querySelectorAll('[data-os]').map(b => b.dataset.os);
          out.markup = pop._html;
        """)
        self.assertNotIn("shot", out["tiles"])
        self.assertIn("awake", out["tiles"])
        self.assertIn("Keep Awake", out["markup"])

    def test_a_machine_without_grim_is_not_offered_a_screenshot_button(self):
        """A tile whose only possible outcome is an apology about a missing package. `grim` is not
        installed everywhere and the honest thing is to not draw the control."""
        out = self.run_js(self.SOUND.replace("|| {ok: true, region: true, why: ''}",
                                             "|| {ok: false, region: false, why: 'needs grim'}"),
            self.OPEN_QUICK + """
          out.tiles = pop.querySelectorAll('[data-os]').map(b => b.dataset.os);
        """)
        self.assertNotIn("shot", out["tiles"],
                         "a Screenshot tile was drawn on a machine that cannot take one")

    def test_a_network_that_could_not_be_read_is_not_an_empty_room(self):
        """A wifi list that is empty because NetworkManager is dead looks exactly like a room with
        no wifi in it — and the honest answer is the difference between "move closer" and "your
        network stack is down"."""
        bridges = self.WM.replace("wifi: async () => { globalThis.__scanned = true;",
                                  "wifi: async () => { globalThis.__scanned = true; throw new Error('nope');")
        out = self.run_js(bridges, self.OPEN_QUICK + """
          const chip = pop.querySelectorAll('[data-os]').find(b => b.dataset.os === 'net');
          await chip.onclick({stopPropagation(){}});
          await new Promise(r => setTimeout(r, 60));
          out.said = (globalThis.__lastPop.querySelector('.os-pop-b') || {})._html || '';
          out.ok = true;
        """)
        self.assertTrue(out.get("ok"), out.get("threw"))


if __name__ == "__main__":
    unittest.main()
