"""The PosterChanOS terminal welcome: what it says, how fast it says it, and how it behaves narrow.

Run: venv-unified/bin/python -m pytest tests/test_posterfetch.py

It is the first thing the machine says on a new local tab, and it is allowed to look like something
— but it is drawn in front of a prompt somebody is waiting for, so the two properties that matter
are that it is FAST and that it never wraps. Everything it prints is computed from values already in
hand: no subprocess, no probe, no await.

The labels are asserted with their separator, not as bare words. `assertIn('OS', out)` used to pass
against a banner with no OS row at all, because "P-O-S-TERCHAN" contains it — a test that cannot
fail is not a test.
"""
import json
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _render(env_js, cols="undefined", timed=False):
    js = ("const start=Date.now();\n"
          "const p=require('./desktop/posterfetch.js');\n"
          f"const out=p.render({env_js}, {cols});\n"
          "console.log(JSON.stringify({out, ms:Date.now()-start}));\n")
    return json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True))


ENV = "{USER:'cyber',HOME:process.env.HOME,SHELL:'/bin/bash',XDG_SESSION_TYPE:'wayland'}"
ENV_TRUE = ("{USER:'cyber',HOME:process.env.HOME,SHELL:'/bin/bash',"
            "XDG_SESSION_TYPE:'wayland',COLORTERM:'truecolor'}")


class PosterfetchTests(unittest.TestCase):
    def test_render_is_fast_self_contained_and_has_the_promised_stats(self):
        got = _render(ENV, cols=120)
        plain = ANSI.sub("", got["out"])
        for label in ("os", "kernel", "uptime", "cpu", "ram", "gpu", "disk", "network", "session"):
            # The label AND a value after it. A row whose value failed to compute is the failure
            # this is about, and a bare label match cannot see it.
            # The logo occupies the start of the line in a wide tab, so the label is anchored on
            # the run of spaces before it rather than on the margin.
            m = re.search(rf"(?:^|\s){label}\s+(\S.*)$", plain, re.M)
            self.assertTrue(m, f"no {label} row")
            self.assertTrue(m.group(1).strip(), f"the {label} row is empty")
        self.assertIn("POSTERCHAN // OWN YOUR SIGNAL", plain)
        self.assertIn("cyber@", plain)
        self.assertLess(got["ms"], 1000)

    def test_it_never_wraps_the_terminal_it_is_printed_into(self):
        """A banner wider than the tab is not a banner, it is debris in front of the first prompt —
        and a pane on a tiling desktop can be any width. The logo is what gives way."""
        for cols in (40, 50, 60, 80, 120, 200):
            plain = ANSI.sub("", _render(ENV, cols=cols)["out"])
            over = [ln for ln in plain.split("\r\n") if len(ln) > cols]
            self.assertEqual(over, [], f"at {cols} columns these lines overflow: {over[:2]}")

    def test_a_narrow_tab_drops_the_logo_and_keeps_the_facts(self):
        """Dropping the FACTS instead would be the wrong half: they are what somebody reads."""
        narrow = ANSI.sub("", _render(ENV, cols=44)["out"])
        self.assertNotIn("/  \\_/  \\", narrow, "the logo survived into a tab too narrow for it")
        self.assertIn("kernel", narrow)
        wide = ANSI.sub("", _render(ENV, cols=120)["out"])
        self.assertIn("/  \\_/  \\", wide, "a wide tab lost the logo")

    def test_the_logo_is_the_posterchan_anime_mascot_not_the_old_circle_mark(self):
        wide = ANSI.sub("", _render(ENV_TRUE, cols=120)["out"])
        self.assertIn("/\\   /\\", wide, "the mascot lost her cat ears")
        self.assertIn("^   ^", wide, "the mascot lost her closed-eye anime expression")
        self.assertIn("/~~~", wide, "the mascot lost the long-hair silhouette")
        self.assertNotIn("▄████", wide, "the old cross-circle mark came back")

    def test_the_logo_rows_are_all_one_width(self):
        """A row that is one cell short shears the fact column beside it, and only on the machine
        whose uptime happens to be long enough to notice."""
        js = "const p=require('./desktop/posterfetch.js');console.log(JSON.stringify(p.LOGO.map(r=>[...r].length)))"
        widths = json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True))
        self.assertEqual(len(set(widths)), 1, f"the logo rows are ragged: {widths}")

    def test_truecolor_is_used_when_offered_and_never_when_not(self):
        """The gradient is the whole look and 256-colour mode has no smooth ramp for it, so the
        fallback steps through hand-picked cube entries rather than converting and banding."""
        self.assertIn("\x1b[38;2;", _render(ENV_TRUE, cols=120)["out"])
        plain256 = _render(ENV, cols=120)["out"]
        self.assertNotIn("\x1b[38;2;", plain256, "24-bit colour emitted to a terminal that never "
                                                 "said it could render it")
        self.assertIn("\x1b[38;5;", plain256)

    def test_a_meter_reads_what_it_is_given(self):
        """It is the one thing on screen carrying a warning, so a full disk must not look like an
        empty one merely because the theme is cyan."""
        js = ("const p=require('./desktop/posterfetch.js');const e={COLORTERM:'truecolor'};"
              "console.log(JSON.stringify([p.meter(e,0,100,10),p.meter(e,50,100,10),"
              "p.meter(e,99,100,10),p.meter(e,1,0,10)]))")
        empty, half, full, nodiv = json.loads(
            subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True))
        self.assertIn("  0%", ANSI.sub("", empty))
        self.assertIn(" 50%", ANSI.sub("", half))
        self.assertIn(" 99%", ANSI.sub("", full))
        self.assertIn("  0%", ANSI.sub("", nodiv))        # total 0 → no division by zero, no NaN%
        self.assertNotIn("NaN", nodiv)
        self.assertNotEqual(half.split("▰")[0], full.split("▰")[0],
                            "a nearly-full meter is the same colour as a half-empty one")

    def test_helpers_do_not_lie_at_boundaries(self):
        js = ("const p=require('./desktop/posterfetch.js'); "
              "console.log(JSON.stringify([p.human(1073741824),p.duration(90061)]))")
        got = json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True))
        self.assertEqual(got, ["1.0 GiB", "1d 1h 1m"])

    def test_pci_device_ids_resolve_to_the_actual_gpu_model(self):
        ids = "1002  Advanced Micro Devices\n\t73bf  Navi 21 [Radeon RX 6800/6800 XT]\n"
        js = ("const p=require('./desktop/posterfetch.js');"
              f"console.log(JSON.stringify(p.pciModel({json.dumps(ids)},'1002','73BF')))" )
        got = json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True))
        self.assertEqual(got, "Navi 21 [Radeon RX 6800/6800 XT]")

    def test_amd_model_does_not_depend_on_optional_uevent_pci_id(self):
        """The canonical sysfs vendor/device files remain sufficient when uevent only names the
        driver. This was the path that printed the generic `AMD amdgpu`."""
        ids = "1002  Advanced Micro Devices\n\t744c  Navi 31 [Radeon RX 7900 XT/7900 XTX]\n"
        js = ("const p=require('./desktop/posterfetch.js');"
              f"console.log(JSON.stringify(p.gpuLabel({json.dumps(ids)},'0x1002','0x744c',"
              "'amdgpu','')))" )
        got = json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True))
        self.assertEqual(got, "AMD Navi 31 [Radeon RX 7900 XT/7900 XTX]")

    def test_platform_gpu_product_name_beats_a_generic_driver_fallback(self):
        js = ("const p=require('./desktop/posterfetch.js');"
              "console.log(JSON.stringify(p.gpuLabel('', '0x1002', '0xffff', 'amdgpu', "
              "'AMD Radeon 780M')))" )
        got = json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True))
        self.assertEqual(got, "AMD Radeon 780M")

    def test_generic_amd_driver_fallback_keeps_the_pci_identity(self):
        js = ("const p=require('./desktop/posterfetch.js');"
              "console.log(JSON.stringify(p.gpuLabel('', '0x1002', '0x744c', 'amdgpu', '')))" )
        got = json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True))
        self.assertEqual(got, "AMD GPU [1002:744c]")

    def test_sysfs_fixture_reports_every_gpu_in_card_order_including_equal_models(self):
        ids = ("1002  Advanced Micro Devices\n"
               "\t73bf  Navi 21 [Radeon RX 6800/6800 XT]\n"
               "\t744c  Navi 31 [Radeon RX 7900 XT/7900 XTX]\n")
        files = {
            "/sys/class/drm/card0/device/vendor": "0x1002",
            "/sys/class/drm/card0/device/device": "0x73bf",
            "/sys/class/drm/card0/device/uevent": "DRIVER=amdgpu",
            "/sys/class/drm/card1/device/vendor": "0x1002",
            "/sys/class/drm/card1/device/device": "0x744c",
            "/sys/class/drm/card1/device/uevent": "DRIVER=amdgpu",
            "/sys/class/drm/card2/device/vendor": "0x1002",
            "/sys/class/drm/card2/device/device": "0x744c",
            "/sys/class/drm/card2/device/uevent": "DRIVER=amdgpu",
        }
        js = ("const p=require('./desktop/posterfetch.js');"
              f"const files={json.dumps(files)},ids={json.dumps(ids)};"
              "const got=p.gpuRows(['renderD128','card2','card0','card1'],"
              " f=>files[f]||'',ids);console.log(JSON.stringify(got))")
        got = json.loads(subprocess.check_output(["node", "-e", js], cwd=ROOT, text=True))
        self.assertEqual(got, [
            "AMD Navi 21 [Radeon RX 6800/6800 XT]",
            "AMD Navi 31 [Radeon RX 7900 XT/7900 XTX]",
            "AMD Navi 31 [Radeon RX 7900 XT/7900 XTX]",
        ])

    def test_posterchanos_explicitly_ships_the_pci_model_database(self):
        """A clean minimal image must not depend on another package incidentally installing pci.ids."""
        installer = (ROOT / "os/gentoo.sh").read_text()
        initial = next(line for line in installer.splitlines()
                       if line.startswith('BASE_PACKAGES="') and '$BASE_PACKAGES' not in line)
        self.assertIn("sys-apps/hwdata", initial)

    def test_gpu_probe_does_not_stop_at_the_first_card(self):
        src = (ROOT / "desktop/posterfetch.js").read_text()
        start = src.index("function gpuRows(")
        body = src[start:src.index("function gpu()", start)]
        self.assertIn("rows.push", body)
        self.assertIn("path.join(dir, 'device')", body,
                      "GPU models still depend on optional uevent PCI_ID")
        self.assertNotRegex(body, r"if \(name \|\| driver\) return",
                            "a second GPU would never be inspected")

    def test_network_enumeration_failure_cannot_break_the_banner_or_shell(self):
        js = ("const os=require('os'); os.networkInterfaces=()=>{throw new Error('offline')}; "
              "const p=require('./desktop/posterfetch.js'); "
              "const out=p.render({COLORTERM:'truecolor'},80); "
              "if(!out.includes('offline')) process.exit(2); console.log('offline banner ok')")
        got = subprocess.run(["node", "-e", js], cwd=ROOT, text=True,
                             capture_output=True, timeout=10)
        self.assertEqual(got.returncode, 0, got.stderr or got.stdout)

    def test_each_new_local_tab_buffers_one_welcome(self):
        src = (ROOT / "desktop/localterm.js").read_text()
        self.assertIn("require('./posterfetch.js')", src)
        self.assertIn("buf: welcome", src)
        self.assertIn("seq: welcome.length", src)

    def test_the_banner_is_rendered_at_the_tab_s_real_width(self):
        """It can only decline to wrap if it is told how wide the tab is."""
        src = (ROOT / "desktop/localterm.js").read_text()
        i = src.index("posterfetch.render(")
        self.assertIn("cols", src[i:i + 200], "the welcome is rendered at a guessed width")
        self.assertIn("COLORTERM: 'truecolor'", src,
                      "the far end of this PTY is xterm.js, which does 24-bit colour")


if __name__ == "__main__":
    unittest.main()
