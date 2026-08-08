"""The desktop app's Tor launcher (desktop/tor.js), driven under node with `electron` stubbed.

Every assertion here is a way the feature would appear to work while doing nothing:

  geoip           `ExitNodes {us}` without GeoIPFile/GeoIPv6File: tor starts, bootstraps, reports 100%
                  and routes wherever it likes. The exit-country picker becomes decoration and NOTHING
                  reports a problem. This is the single most important line in the file.
  strict-pairing  StrictNodes belongs with ExitNodes and only there. With a country it is what makes the
                  country a guarantee (tor refuses rather than quietly exiting elsewhere); WITHOUT one it
                  is meaningless, and pinning it on regardless would constrain circuits the user never
                  asked to constrain.
  ephemeral-ports SocksPort/ControlPort must not be 9050/9051. A Tor user already runs a system tor, and
                  the collision surfaces as "tor exited immediately", i.e. as a broken app.
  country-guard   Only a two-letter code or '' may reach torrc — it is interpolated into a config file.
  fail-closed     When tor dies while enabled, the state must keep `enabled` true and record an error.
                  Flipping `enabled` to false is what would let main.js clear the proxy and silently put
                  the user back on the clear net.
  bootstrap       Progress is parsed off tor's stdout ("Bootstrapped 45% ..."), which is the only reason
                  the log level is notice and the sink is stdout.
  no-exit-country A country with no usable exits must produce a NAMED error, not a progress bar that
                  never moves — it is the one failure the user caused and can fix.
"""
import json
import os
import shutil
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(ROOT, "desktop")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run_js(tmp_path, body, crash_after=None):
    """Run `body` with desktop/tor.js loaded and `electron` + a fake `tor` binary in place.

    The stub matters as much as the test: tor.js reaches for app.getPath('userData'),
    app.isPackaged and __dirname/resources/tor, so a stub that got those wrong would test nothing.

    `crash_after` makes the fake tor exit non-zero after that many seconds. Killing the child from
    the test instead would mean pkill'ing a pattern that also matches the shell running it — the
    first attempt at this test killed its own harness and reported a tor.js bug that wasn't there.
    """
    ud = tmp_path / "userdata"
    ud.mkdir()
    res = tmp_path / "resources" / "tor"
    (res / "tor").mkdir(parents=True)
    (res / "data").mkdir(parents=True)
    (res / "data" / "geoip").write_text("# fake geoip\n")
    (res / "data" / "geoip6").write_text("# fake geoip6\n")

    # A "tor" that prints a bootstrap sequence to stdout and stays up, so start() has a live child to
    # manage and the stdout parser has something real to parse.
    # All three Bootstrapped lines are echoed back to back ON PURPOSE: they then arrive in ONE stdout
    # chunk, which is what a fast real connection does and what a per-chunk single .exec() got wrong
    # (progress stuck at the first match, 100% never seen, boot card hung until its timeout).
    fake = res / "tor" / "tor"
    tail = f"sleep {crash_after}\nexit 1\n" if crash_after else "sleep 30\n"
    # It records the environment it was GIVEN before saying anything. tor's own shared libraries are
    # found (or not) by the dynamic loader before main() runs, so nothing tor prints can tell you
    # whether that was set up right — the only way to test it is to look at what was handed to the
    # process. `which` names the binary that ran, for the per-architecture lookup.
    probe = textwrap.dedent("""\
        D="$(dirname "$0")"
        printf '%s' "${LD_LIBRARY_PATH-}" > "$D/../ldpath.txt"
        printf '%s' "$0" > "$D/../which.txt"
    """)
    fake.write_text(textwrap.dedent("""\
        #!/bin/sh
    """) + probe + textwrap.dedent("""\
        echo "Feb 01 00:00:00.000 [notice] Bootstrapped 10% (conn): Connecting to a relay"
        echo "Feb 01 00:00:00.000 [notice] Bootstrapped 45% (requesting_descriptors): Asking for descriptors"
        echo "Feb 01 00:00:00.000 [notice] Bootstrapped 100% (done): Done"
    """) + tail)
    fake.chmod(0o755)

    stub = tmp_path / "node_modules" / "electron"
    stub.mkdir(parents=True)
    (stub / "package.json").write_text(json.dumps({"name": "electron", "main": "index.js"}))
    (stub / "index.js").write_text(f"""
      module.exports = {{ app: {{
        isPackaged: false,
        getPath: () => {json.dumps(str(ud))},
      }} }};
    """)

    entry = tmp_path / "entry.js"
    entry.write_text(f"""
      const path = require('path');
      const tor = require({json.dumps(os.path.join(DESKTOP, 'tor.js'))});
      const RES = {json.dumps(str(tmp_path / 'resources'))};
      const TORRC = path.join({json.dumps(str(ud))}, 'tor', 'torrc');
      const fs = require('fs');
      (async () => {{
      {body}
      }})().then(() => process.exit(0)).catch(e => {{ console.error('ERR ' + (e && e.stack || e)); process.exit(1); }});
    """)

    # tor.js resolves the bundle from __dirname/resources/tor in dev, so run with cwd=tmp_path and a
    # symlinked resources/ next to a COPY of tor.js — simpler: point __dirname at the real desktop dir
    # and give it a resources/ there? No: never write into the repo from a test. Instead copy tor.js.
    shutil.copy(os.path.join(DESKTOP, "tor.js"), tmp_path / "tor.js")
    entry.write_text(entry.read_text().replace(
        json.dumps(os.path.join(DESKTOP, "tor.js")), json.dumps(str(tmp_path / "tor.js"))))

    r = subprocess.run(["node", str(entry)], cwd=str(tmp_path), capture_output=True, text=True,
                       timeout=90)
    assert r.returncode == 0, f"node failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout


def _torrc(tmp_path, country):
    out = run_js(tmp_path, f"""
      await tor.init({{ enabled: true, country: {json.dumps(country)} }});
      // Give the fake tor a moment to emit its bootstrap lines.
      await new Promise(r => setTimeout(r, 1200));
      console.log('TORRC<<<' + fs.readFileSync(TORRC, 'utf8') + '>>>');
      console.log('STATUS<<<' + JSON.stringify(tor.status()) + '>>>');
      tor.stop();
    """)
    rc = out.split("TORRC<<<")[1].split(">>>")[0]
    st = json.loads(out.split("STATUS<<<")[1].split(">>>")[0])
    return rc, st


def test_geoip_is_written_or_the_country_picker_is_decoration(tmp_path):
    rc, _ = _torrc(tmp_path, "us")
    assert "GeoIPFile " in rc, (
        "no GeoIPFile in torrc — tor cannot map ExitNodes {us} to relays, so it bootstraps to 100% and "
        "exits in whatever country it likes while the UI claims otherwise:\n" + rc)
    assert "GeoIPv6File " in rc, "no GeoIPv6File in torrc:\n" + rc


def test_country_brings_exitnodes_and_strictnodes_together(tmp_path):
    rc, st = _torrc(tmp_path, "de")
    assert "ExitNodes {de}" in rc, rc
    assert "StrictNodes 1" in rc, (
        "StrictNodes missing alongside ExitNodes — the country becomes a preference tor may silently "
        "ignore, not the guarantee Settings promises:\n" + rc)
    assert st["country"] == "de"
    assert st["countryName"] == "Germany"


def test_no_country_means_no_strictnodes(tmp_path):
    rc, st = _torrc(tmp_path, "")
    assert "ExitNodes" not in rc, rc
    assert "StrictNodes" not in rc, (
        "StrictNodes written with no ExitNodes — meaningless on its own, and it constrains circuit "
        "building the user never asked to constrain:\n" + rc)
    assert st["country"] == ""


def test_ports_are_ephemeral_not_the_system_tor_defaults(tmp_path):
    rc, st = _torrc(tmp_path, "")
    assert "SocksPort 127.0.0.1:9050" not in rc, (
        "SOCKS on the default 9050 collides with a system tor — which is exactly what a Tor user runs — "
        "and the collision looks like 'tor exited immediately':\n" + rc)
    assert "ControlPort 127.0.0.1:9051" not in rc, rc
    assert st["socksPort"] > 1024
    # Bound to loopback only, never a LAN interface: an open SOCKS proxy is an open relay.
    assert "SocksPort 127.0.0.1:" in rc and "ControlPort 127.0.0.1:" in rc, rc
    assert "CookieAuthentication 1" in rc, "no control cookie — 'New circuit' (NEWNYM) cannot authenticate"
    assert "Log notice stdout" in rc, "bootstrap progress is parsed off stdout at notice level"


def test_a_bogus_country_never_reaches_torrc(tmp_path):
    out = run_js(tmp_path, """
      await tor.init({ enabled: false, country: 'not-a-country' });
      const a = tor.status().country;
      await tor.set({ country: '../../etc/passwd' });
      const b = tor.status().country;
      await tor.set({ country: 'JP' });
      const c = tor.status().country;
      console.log('CC<<<' + JSON.stringify([a, b, c]) + '>>>');
    """)
    a, b, c = json.loads(out.split("CC<<<")[1].split(">>>")[0])
    assert a == "", f"a junk saved country should fall back to any, got {a!r}"
    assert b == "", f"a path-traversal country reached the state: {b!r}"
    assert c == "jp", f"a valid uppercase code should normalise to lowercase, got {c!r}"


def test_bootstrap_progress_is_parsed_from_stdout(tmp_path):
    out = run_js(tmp_path, """
      await tor.init({ enabled: true, country: '' });
      await new Promise(r => setTimeout(r, 1500));
      console.log('ST<<<' + JSON.stringify(tor.status()) + '>>>');
      tor.stop();
    """)
    st = json.loads(out.split("ST<<<")[1].split(">>>")[0])
    assert st["progress"] == 100, f"bootstrap progress not parsed off stdout: {st}"
    assert st["bootstrapped"] is True, st
    assert st["running"] is True, st


def test_tor_dying_fails_closed(tmp_path):
    """The whole promise of the switch. If `enabled` flipped to false here, main.js's applyProxy would
    clear the SOCKS proxy and put the user back on the clear net without a word."""
    out = run_js(tmp_path, """
      await tor.init({ enabled: true, country: '' });
      await new Promise(r => setTimeout(r, 1000));
      const before = tor.status();
      // The fake tor exits non-zero by itself (crash_after) — which is precisely what a crash looks
      // like from tor.js's side, without the harness having to kill anything.
      await new Promise(r => setTimeout(r, 2500));
      const after = tor.status();
      console.log('BA<<<' + JSON.stringify({before, after}) + '>>>');
    """, crash_after=2)
    d = json.loads(out.split("BA<<<")[1].split(">>>")[0])
    assert d["before"]["running"] is True, d
    assert d["after"]["running"] is False, d
    assert d["after"]["enabled"] is True, (
        "tor died and `enabled` went false — main.js would then clear the SOCKS proxy and silently put "
        f"the user on the clear net: {d['after']}")
    assert d["after"]["error"], f"a crash must be reported, not silent: {d['after']}"
    assert d["after"]["bootstrapped"] is False, d


def test_turning_it_off_is_not_an_error(tmp_path):
    out = run_js(tmp_path, """
      await tor.init({ enabled: true, country: 'us' });
      await new Promise(r => setTimeout(r, 1200));
      const s = await tor.set({ enabled: false });
      console.log('OFF<<<' + JSON.stringify(s) + '>>>');
    """)
    s = json.loads(out.split("OFF<<<")[1].split(">>>")[0])
    assert s["enabled"] is False and s["running"] is False, s
    assert s["error"] == "", f"a deliberate switch-off must not leave an error behind: {s}"
    # The country survives being switched off, so turning it back on keeps the user's choice.
    assert s["country"] == "us", s


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"),
                    reason="LD_LIBRARY_PATH is a Linux-only concern (see spawnEnv)")
def test_linux_spawns_tor_with_its_OWN_libraries(tmp_path):
    """The bundled Linux tor has no RPATH and no RUNPATH — verified with readelf on the real 14.5.8
    expert bundle — and ld.so has never searched the working directory. So without LD_LIBRARY_PATH the
    feature fails one of two ways, and the second is the nasty one:

      no system libevent  → "error while loading shared libraries: libevent-2.1.so.7"
      a system libevent   → it loads the WRONG build and dies on a symbol that build does not export:
                            "symbol lookup error: undefined symbol: evutil_secure_rng_add_bytes".
                            Reproduced on a stock distribution with the real binary; most Linux desktops
                            have libevent, so this was the common case, not the edge one.

    Both reach the user as "Tor stopped unexpectedly (exit 127)". The value must also be EXACTLY the
    bundle directory, not the bundle appended to whatever was inherited: an AppImage exports its own
    library directory in LD_LIBRARY_PATH, and its libssl shadowing tor's is the same failure with a
    different library name."""
    run_js(tmp_path, """
      await tor.init({ enabled: true, country: '' });
      await new Promise(r => setTimeout(r, 1200));
      tor.stop();
    """)
    got = (tmp_path / "resources" / "tor" / "ldpath.txt").read_text()
    want = str(tmp_path / "resources" / "tor" / "tor")
    assert got == want, (
        f"tor was spawned with LD_LIBRARY_PATH={got!r}, want exactly {want!r} — its bundled "
        "libevent/libssl/libcrypto sit there and the loader looks nowhere else")


def test_the_architecture_subdirectory_wins(tmp_path):
    """macOS ships two tor binaries (x86_64 + arm64) in one packaged resources tree, because a .dmg is
    built per architecture off the same tree and a Mach-O is built for one. tor.js picks by
    process.arch and falls back to the base tree — get the preference backwards and an Apple Silicon
    Mac runs the Intel binary, which needs Rosetta, which is not installed until something asks for
    it. A native arm64 app never asks, so tor asks first and fails to exec."""
    out = run_js(tmp_path, """
      const arch = path.join(RES, 'tor', process.arch, 'tor');
      fs.mkdirSync(arch, { recursive: true });
      const src = path.join(RES, 'tor', 'tor', 'tor');
      fs.writeFileSync(path.join(arch, 'tor'), fs.readFileSync(src));
      fs.chmodSync(path.join(arch, 'tor'), 0o755);
      await tor.init({ enabled: true, country: '' });
      await new Promise(r => setTimeout(r, 1200));
      console.log('ARCH<<<' + process.arch + '>>>');
      tor.stop();
    """)
    arch = out.split("ARCH<<<")[1].split(">>>")[0]
    ran = (tmp_path / "resources" / "tor" / arch / "which.txt").read_text()
    assert ran == str(tmp_path / "resources" / "tor" / arch / "tor" / "tor"), (
        f"the base-tree binary ran while an {arch} one was present: {ran}")


def test_a_binary_that_cannot_run_is_an_error_not_a_dead_app(tmp_path):
    """A ChildProcess is an EventEmitter, so an 'error' event with no listener is re-thrown as an
    uncaught exception — in Electron that takes the MAIN PROCESS down, i.e. the window disappears on
    the click that turned Tor on, with nothing to read anywhere. The ways a spawn never happens are
    all real: a binary the installer failed to mark executable, a /tmp mounted noexec under an
    AppImage, and an x86_64 tor on an Apple Silicon Mac with no Rosetta (EBADARCH).

    The node process exiting 0 IS half this assertion — run_js fails the test on a non-zero exit, which
    is what an unhandled 'error' would produce."""
    out = run_js(tmp_path, """
      fs.chmodSync(path.join(RES, 'tor', 'tor', 'tor'), 0o644);   // present, not executable
      await tor.init({ enabled: true, country: '' });
      await new Promise(r => setTimeout(r, 800));
      console.log('S<<<' + JSON.stringify(tor.status()) + '>>>');
    """)
    s = json.loads(out.split("S<<<")[1].split(">>>")[0])
    assert s["error"], f"a tor that could not be started must say so: {s}"
    assert s["running"] is False, s
    assert s["enabled"] is True, (
        "the switch must stay on so the proxy stays pointed at a dead port — see fail-closed")


def test_a_startup_failure_says_what_tor_said(tmp_path):
    """"exit 127" is not a bug report. Tor names the reason on its way out — a missing shared library,
    a too-permissive DataDirectory — and throwing that away is why the Linux library bug above took a
    readelf to find rather than a screenshot."""
    out = run_js(tmp_path, """
      // Replace the harness's fake with one that dies the way the real thing did (run_js writes it,
      // so this has to happen here rather than before the call).
      const bin = path.join(RES, 'tor', 'tor', 'tor');
      fs.writeFileSync(bin, "#!/bin/sh\\n"
        + "echo 'error while loading shared libraries: libevent-2.1.so.7' >&2\\n"
        + "exit 127\\n");
      fs.chmodSync(bin, 0o755);
      await tor.init({ enabled: true, country: '' });
      await new Promise(r => setTimeout(r, 900));
      console.log('S<<<' + JSON.stringify(tor.status()) + '>>>');
    """)
    s = json.loads(out.split("S<<<")[1].split(">>>")[0])
    assert "libevent" in s["error"], f"tor's own last words must reach the user: {s['error']!r}"


def test_countries_are_offered_with_any_available(tmp_path):
    out = run_js(tmp_path, """
      await tor.init({ enabled: false, country: '' });
      const s = tor.status();
      console.log('C<<<' + JSON.stringify({n: s.countries.length, first: s.countries[0],
                                           avail: s.available}) + '>>>');
    """)
    d = json.loads(out.split("C<<<")[1].split(">>>")[0])
    assert d["n"] >= 20, f"too few exit countries offered: {d['n']}"
    assert len(d["first"]) == 2 and len(d["first"][0]) == 2, d
    assert d["avail"] is True, "the bundled binary was not found — tor.available() gates the whole panel"


def test_the_window_does_not_throttle_a_live_stream():
    """Chromium throttles timers and rendering in a non-foreground window; a broadcast cannot take it.

    Put a game in front of the app and the player stops being serviced, falls behind the live edge,
    then catches up when you return — a black frame on switching, and stuttering while it is behind.
    Reported as "windows app stuttery, firefox smooth and perfect": same machine, same stream, same
    moment, same client code. Firefox does not throttle a visible-but-unfocused window the way
    Chromium does, and that was the whole of the difference.

    The default is ON, so this has to be set explicitly — which means it can be silently lost by any
    edit to webPreferences.
    """
    src = open(os.path.join(ROOT, "desktop", "main.js"), encoding="utf-8").read()
    i = src.index("webPreferences: {")
    block = src[i:src.index("preload:", i)]      # the whole webPreferences literal
    assert "backgroundThrottling: false" in block, (
        "the main window throttles when it loses focus — a live stream falls behind the moment "
        "anything covers it")
