"""The recovery shortcut, the launcher it drives, and the surfaces it has to bring back.

PORTED FROM SWAY. Every assertion here used to read `sway.config`, `os/bin/pc-shell-start` and the
~150 lines of per-account config migration in the ebuild -- all three are gone. What is NOT gone is
what they were protecting, and each rule below cost a real desktop at least once:

  * Ctrl+Alt+Backspace restarts the SHELL, never the session. Bound to `swaymsg exit` or a
    `systemctl` unit it logs the user out, which is not what the key is for.
  * It targets the canonical shell PROCESS and nothing else. `pkill -f /opt/posterchan` also
    catches an installed diagnostic instance, so an update test could tear down the real desktop.
  * The launcher is the only thing allowed to start a shell: it serializes, clears dead singleton
    locks, waits for the display socket and PROVES a surface mapped.
  * The restart's replacement surfaces are navigated and shown in a fixed order, and a navigation
    that failed never claims a black window recovered.

`shell-recovery.js` is compositor-neutral and its node tests are carried over verbatim.
"""
from pathlib import Path
import json
import re
import subprocess

from tests.wayfire_config import bindings, runs


ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "os/overlay/app-misc/posterchanos-shell/files"
EBUILD = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()


def test_ctrl_alt_backspace_restarts_only_the_posterchan_shell():
    chords = runs("pc-shell-restart")
    assert chords == ["<ctrl> <alt> KEY_BACKSPACE"], chords
    command = bindings()["<ctrl> <alt> KEY_BACKSPACE"]
    assert command == "/usr/local/bin/pc-shell-restart"
    # The two ways this shortcut has historically been mis-implemented: ending the compositor, and
    # bouncing a unit. Both log the user out to restart one Electron process.
    for wrong in ("exit", "systemctl", "wayfire"):
        assert wrong not in command, command


def test_the_session_config_is_the_only_place_the_binding_comes_from():
    """gentoo.sh used to generate a SECOND copy of every binding, which then drifted.

    The installer now installs the package that ships `wayfire.ini` and writes no compositor config
    of its own, so there is exactly one file to keep correct.
    """
    installer = (ROOT / "os/gentoo.sh").read_text()
    assert "bindsym" not in installer, "the installer generates compositor bindings again"
    assert "wayfire.ini" in installer


def test_shell_package_installs_the_config_name_the_compositor_actually_reads():
    assert 'doins "${FILESDIR}/wayfire.ini"' in EBUILD
    assert "insinto /etc\n" in EBUILD
    # pc-compositor-session launches `wayfire -c /etc/wayfire.ini`; anything else is a file the
    # compositor never opens.
    session = (ROOT / "os/bin/pc-compositor-session").read_text()
    assert 'PC_WAYFIRE_CONFIG:-/etc/wayfire.ini' in session


def test_no_per_account_compositor_config_is_written_or_migrated():
    """Provisioning gave every identity a private copy of the compositor config, so each upgrade had
    to reach in and rewrite package-owned bindings inside it. One package-owned file replaces all of
    that; the only thing left is retiring the old directory rather than deleting somebody's file."""
    switch = (ROOT / "os/bin/pc-session-switch").read_text()
    assert ".config/sway" not in switch
    assert "exec /usr/local/bin/pc-compositor-session" in switch
    assert 'mv -T "${cfg}" "${cfg}.retired-sway"' in EBUILD
    assert "cat >>\"${cfg}\"" not in EBUILD, "an upgrade edits a private compositor config again"


def test_shell_restart_targets_only_the_shell_process_and_stops_its_launcher():
    restart = (FILES / "pc-shell-restart").read_text()
    main = (ROOT / "desktop/main.js").read_text()
    assert "candidates=$requested" in restart
    assert "pgrep -f '[/]opt/posterchan/posterchan-desktop'" in restart
    assert "/opt/posterchan/posterchan-desktop\\ *--shell*" in restart
    # An installed diagnostic instance runs the same executable in its own singleton domain.
    assert "--pc-diagnostic-token=" in restart
    assert "--pc-diagnostic-profile=" in restart
    assert "exit 64" in restart
    assert "kill $pids" in restart
    assert "pkill" not in restart
    assert "send_tick" not in restart, "the Sway-only IPC restart path came back"
    # THE LAUNCHER IS KILLED FIRST. It supervises the shell and answers an exiting one by retrying,
    # so killing only the shell made the old launcher race this script's replacement for the same
    # singleton and flock; both attempts burned inside a second and the login ended at a console.
    assert "pc-shell-start-wayfire*" in restart
    assert "kill $launchers" in restart
    assert 'PC_SHELL_START:=/usr/local/bin/pc-shell-start-wayfire' in restart
    assert 'exec "$PC_SHELL_START"' in restart
    assert "/usr/local/bin/pc-shell-start\n" not in restart, "the retired Sway launcher is back"
    # The supervisor is told this is a restart BEFORE the shell dies, or its 3-second crash window
    # fires against a launcher whose own startup is bounded at 40.
    assert "posterchan-shell-restarting" in restart
    assert "recoverSurfaces(_shellSurfaces.values(), loadApp).catch" in main


def test_the_launcher_is_the_only_thing_that_starts_a_shell():
    start = (FILES / "pc-shell-start-wayfire").read_text()
    assert not (ROOT / "os/bin/pc-shell-start").exists(), "the Sway launcher is shipped again"
    # ONE shell per session, and a live one is never displaced.
    assert "A PosterChan shell already belongs to another session" in start
    launches = re.findall(r'"\$launcher" --shell --ozone-platform=wayland[^\n]*', start)
    assert len(launches) == 1, launches
    # The launcher mutex is not part of the desktop environment: anything Electron spawns would
    # inherit fd 9 and hold it.
    assert all("9>&-" in line for line in launches), launches
    assert 'PC_SHELL_EXTRA_ARGS:-' in launches[0]
    # Dead singleton state is cleared; a live one is not, because it belongs to a running shell.
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        assert lock in start
    # The display socket is FOUND, not assumed: an ssh-launched or early start has no
    # WAYLAND_DISPLAY, and Electron then silently falls back to X11 and exits.
    assert 'find "$XDG_RUNTIME_DIR" -maxdepth 1 -type s -name' in start
    assert 'DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"' in start
    # Only the SOFT core limit: `ulimit -c 0` lowers the inherited hard limit too and every VM
    # then fails before exec, which is why the libvirt repair rides beside it.
    assert "ulimit -S -c 0" in start
    assert "ulimit -c 0\n" not in start
    assert "max_core = 0" in start


def test_the_launcher_proves_a_surface_mapped_before_signalling_ready():
    """A compositor that is alive with no shell on it is a failure, not a slow start.

    And the signal has to be WRITTEN: pc-compositor-session waits 60s for this file and then kills
    Wayfire with "the shell never signalled ready". Nothing wrote it for the whole life of the
    Wayfire launcher -- it only ever removed it in cleanup -- so a desktop that came up perfectly
    was torn down one minute later and the console blamed the shell.
    """
    start = (FILES / "pc-shell-start-wayfire").read_text()
    after_health = start.split('"$health" wait', 1)[1]
    assert ': >"$PC_WAYFIRE_READY_FILE"' in after_health
    assert "PosterChan shell window mapped" in after_health
    # Ready means VERIFIED, so the bundle-identity re-check comes first: an update that landed while
    # the shell was starting is a different desktop from the one that was measured.
    assert after_health.index("bundle_identity") < after_health.index('>"$PC_WAYFIRE_READY_FILE"')
    session = (ROOT / "os/bin/pc-compositor-session").read_text()
    assert 'rm -f "$PC_WAYFIRE_READY_FILE"' in session
    assert "the shell never signalled ready" in session


def test_a_verified_shell_rearms_the_boot_loop_guard():
    """~/.bash_profile hands out a diagnostic shell after two login attempts per boot. The Sway
    launcher cleared that counter once the shell mapped; the Wayfire one never did, so two logins in
    one boot -- two Ctrl+Alt+Backspaces -- left the THIRD at a text prompt on a healthy machine."""
    start = (FILES / "pc-shell-start-wayfire").read_text()
    after_health = start.split('"$health" wait', 1)[1]
    assert "compositor-boot-attempt" in after_health
    assert 'rm -f "$HOME/.local/state/posterchanos/compositor-boot-attempt"' in after_health


def test_desktop_wrapper_preserves_the_shells_wayland_backend():
    wrapper = (ROOT / "os/bin/posterchan-wrapper").read_text()
    ebuilds = sorted((ROOT / "os/overlay/app-misc/posterchan-desktop").glob("posterchan-desktop-*.ebuild"))
    assert len(ebuilds) == 1, "the overlay should expose one immutable desktop version"
    updater = (ROOT / "os/bin/update-posterchan").read_text()
    for source in (wrapper, ebuilds[0].read_text(), updater):
        assert '${ELECTRON_OZONE_PLATFORM_HINT:=auto}' in source
        assert 'export ELECTRON_OZONE_PLATFORM_HINT=auto' not in source


def test_super_is_a_physical_key_release_not_a_bare_modifier_binding():
    """Super opens Start on RELEASE, and only when it was not used as a modifier.

    Wayfire's `release_binding_start` fires whether or not the key modified something, exactly as
    Sway's `--release` did -- which is why every Super+chord marks it consumed through `pc-super
    used` before doing its own work, and why those chords are chained commands rather than pairs of
    bindings on one chord (only one binding on a chord can win).
    """
    from tests.wayfire_config import sections
    command = sections()["command"]
    assert command["release_binding_start"] == "KEY_LEFTMETA"
    assert command["command_start"] == "/usr/local/bin/pc-super tap"
    for chord, run in bindings().items():
        if chord.startswith("<super>") and chord != "KEY_LEFTMETA":
            assert "pc-super used" in run, (chord, run)
            assert run != "/usr/local/bin/pc-super used", (chord, "marks the modifier and does nothing")


def test_alt_tab_reaches_posterchan_windows_and_not_only_the_compositors_toplevels():
    """Every PosterChan window on a monitor lives inside ONE compositor toplevel.

    So Wayfire's own `switcher` -- which claims <alt>Tab by default -- can only ever offer "the
    desktop" and the native applications beside it, never Messages, Terminal, Code or Files. The
    helper and the renderer's `pc:cycle:*` handler were both ported and then nothing bound the key.
    """
    from tests.wayfire_config import sections
    assert bindings()["<alt> KEY_TAB"] == "/usr/local/bin/pc-window-cycle next"
    assert bindings()["<alt> <shift> KEY_TAB"] == "/usr/local/bin/pc-window-cycle previous"
    plugins = sections()["core"]["plugins"].split()
    assert "switcher" not in plugins, "the compositor's switcher takes <alt>Tab back"
    assert (FILES / "pc-window-cycle").exists()
    assert "pc-window-cycle" in EBUILD
    assert "pc:cycle:" in (ROOT / "static/js/client/os.js").read_text()


def test_native_windows_have_a_close_shortcut_that_does_not_kill_the_desktop():
    """AND BOTH CLOSE CHORDS ANSWER THE SAME WAY.

    Sway's bare `kill` closed the focused container, which is the single shell surface hosting every
    PosterChan window -- Alt+F4 destroyed the session. The replacement asks what is focused first.
    `pc-wayfire-action pc:close` is NOT that: it reaches only the renderer's own focused frame, so
    with a popped-out window (a toplevel in another renderer) or a bare native surface focused it
    closed nothing, and the key read as broken.
    """
    b = bindings()
    assert b["<alt> KEY_F4"] == "/usr/local/bin/pc-window-close"
    assert b["<super> KEY_Q"].endswith("/usr/local/bin/pc-window-close")
    assert "pc:close" not in " ".join(b.values()), "a close chord bypasses the focus question"
    close = (FILES / "pc-window-close").read_text()
    assert "exec /usr/local/bin/pc-window-snap close" in close, "a second copy of the same helper"


def test_restart_navigates_a_secondary_surface_that_is_still_about_blank():
    helper = ROOT / "desktop/shell-recovery.js"
    code = f"""
      const {{recoverSurfaces}}=require({json.dumps(str(helper))});
      (async()=>{{
        const b={{url:'about:blank',shown:false,isDestroyed:()=>false,show(){{this.shown=true}}}};
        const n=await recoverSurfaces([{{browser:b}}], x=>{{x.url='https://poster.place/client'}});
        process.stdout.write(JSON.stringify({{n,url:b.url,shown:b.shown}}));
      }})().catch(e=>{{console.error(e);process.exit(1)}});
    """
    got=json.loads(subprocess.check_output(["node","-e",code],text=True))
    assert got == {"n":1,"url":"https://poster.place/client","shown":True}


def test_restart_reloads_two_live_monitors_sequentially():
    helper = ROOT / "desktop/shell-recovery.js"
    code = f"""
      const {{recoverSurfaces}}=require({json.dumps(str(helper))});
      let active=0,max=0,order=[];
      function browser(name){{
        const listeners={{}};
        return {{isDestroyed:()=>false,show(){{order.push('show-'+name)}},webContents:{{
          getURL:()=> 'https://poster.place/client',
          once:(ev,fn)=>{{listeners[ev]=fn}},
          reloadIgnoringCache:()=>{{active++;max=Math.max(max,active);order.push('load-'+name);
            setTimeout(()=>{{active--;listeners['did-finish-load']()}},5)}}
        }}}};
      }}
      (async()=>{{const n=await recoverSurfaces([{{browser:browser('a')}},{{browser:browser('b')}}],()=>{{}});
        process.stdout.write(JSON.stringify({{n,max,order}}));}})();
    """
    got=json.loads(subprocess.check_output(["node","-e",code],text=True))
    assert got == {"n":2,"max":1,"order":["load-a","show-a","load-b","show-b"]}


def test_a_failed_reload_is_canonically_navigated_before_the_surface_is_shown():
    helper = ROOT / "desktop/shell-recovery.js"
    code = f"""
      const {{recoverSurfaces}}=require({json.dumps(str(helper))});
      let failed;
      const listeners={{}}, order=[];
      const b={{isDestroyed:()=>false,show:()=>order.push('show'),webContents:{{
        getURL:()=> 'https://poster.place/client',
        once:(ev,fn)=>{{listeners[ev]=fn}},
        reloadIgnoringCache:()=>setTimeout(()=>listeners['did-fail-load'](),1)
      }}}};
      (async()=>{{const n=await recoverSurfaces([{{browser:b}}],async()=>{{
        order.push('navigate-start'); await new Promise(r=>setTimeout(r,5)); order.push('navigate-end');
      }}); process.stdout.write(JSON.stringify({{n,order}}));}})();
    """
    got=json.loads(subprocess.check_output(["node","-e",code],text=True))
    assert got == {"n":1,"order":["navigate-start","navigate-end","show"]}


def test_a_failed_canonical_navigation_never_claims_the_black_surface_recovered():
    helper = ROOT / "desktop/shell-recovery.js"
    code = f"""
      const {{recoverSurfaces}}=require({json.dumps(str(helper))});
      const b={{url:'about:blank',shown:false,isDestroyed:()=>false,show(){{this.shown=true}}}};
      (async()=>{{const n=await recoverSurfaces([{{browser:b}}],()=>Promise.reject(new Error('no paint')));
        process.stdout.write(JSON.stringify({{n,shown:b.shown}}));}})();
    """
    got=json.loads(subprocess.check_output(["node","-e",code],text=True))
    assert got == {"n":0,"shown":False}


def test_the_canonical_shell_navigation_is_awaited():
    main = (ROOT / "desktop/main.js").read_text()
    assert "await target.loadURL(APP_URL);" in main


def test_a_restart_that_did_not_inherit_the_session_still_finds_the_compositor():
    """REFUSING HERE DOES NOT FAIL THE RESTART — IT ENDS THE DESKTOP.

    The launcher used to exit when `WAYFIRE_SOCKET` was unset, which is every launch that does not
    inherit the session environment: an ssh-driven `pc-shell-restart`, a recovery start, anything
    run from a service. And pc-shell-restart EXECS the launcher, so that refusal means there is now
    no shell at all — the supervisor waits for a replacement PID, none arrives, and it stops Wayfire
    and drops the login to a text console. Measured from exactly one remote restart on the real
    machine: `no replacement shell within 60000ms of the last one exiting; stopping Wayfire`.

    There is one Wayfire session per user runtime directory and its socket is named for it, so this
    is a search, not a guess — the same recovery the WAYLAND_DISPLAY block already does.
    """
    start = (FILES / "pc-shell-start-wayfire").read_text()
    head = start.split("exec 9>", 1)[0]
    assert "find \"$XDG_RUNTIME_DIR\" -maxdepth 1 -type s -name 'wayfire-*.socket'" in head, head
    assert "export WAYFIRE_SOCKET" in head
    # It still refuses when there is genuinely no compositor — a shell started against nothing is a
    # black screen, not a desktop.
    assert "Wayfire IPC socket is unavailable" in head


def test_the_recovery_runs_before_the_launcher_mutex():
    """The flock is taken for the duration of a start. Discovering the socket after it would hold
    the lock across a failure that has already decided to exit."""
    start = (FILES / "pc-shell-start-wayfire").read_text()
    assert start.index("wayfire-*.socket") < start.index('exec 9>"$XDG_RUNTIME_DIR/posterchan-shell-start.lock"')
