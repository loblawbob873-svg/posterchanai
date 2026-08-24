from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_ctrl_alt_backspace_restarts_only_the_posterchan_shell():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    line = next(x for x in cfg.splitlines() if "Ctrl+Mod1+BackSpace" in x)
    assert "bindsym --no-repeat" in line
    assert "/usr/local/bin/pc-shell-restart" in line
    assert "swaymsg exit" not in line
    assert "systemctl" not in line


def test_installer_ships_the_same_recovery_binding():
    installer = (ROOT / "os/gentoo.sh").read_text()
    assert "bindsym --no-repeat Ctrl+Mod1+BackSpace" in installer
    assert "/usr/local/bin/pc-shell-restart" in installer


def test_shell_package_installs_the_config_name_sway_actually_reads():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert 'newins "${FILESDIR}/sway.config" config' in ebuild
    assert 'doins "${FILESDIR}/sway.config"' not in ebuild


def test_shell_package_migrates_existing_identity_configs_without_replacing_them():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert '"${EROOT%/}"/home/pc-*/.config/sway/config' in ebuild
    assert "Ctrl\\+Mod1\\+(BackSpace|22)" in ebuild
    assert "Super_L exec swaymsg -t send_tick pc:start" in ebuild
    assert 'cat >>"${cfg}"' in ebuild


def test_shell_restart_is_serialized_and_targets_only_the_shell_process():
    start = (ROOT / "os/bin/pc-shell-start").read_text()
    restart = (ROOT / "os/bin/pc-shell-restart").read_text()
    main = (ROOT / "desktop/main.js").read_text()
    assert "flock -n 9" in start
    assert "posterchan-shell-start.lock" in start
    assert start.count("/usr/local/bin/posterchan --shell --ozone-platform=wayland 9>&- &") == 2
    assert "pattern='[/]opt/posterchan/'" in restart
    assert "send_tick pc:restart" in restart
    assert "pkill" not in restart
    assert "recoverSurfaces(_shellSurfaces.values(), loadApp).catch" in main
    assert "ev.payload !== 'pc:restart'" in main
    assert "exec /usr/local/bin/pc-shell-start" in restart
    assert "retries" in start and "exit 1" in start
    assert "$USER_HOME/SingletonLock" in start
    assert "clear_dead_locks" in start
    assert "SingletonSocket" in start and "SingletonCookie" in start
    assert "ELECTRON_OZONE_PLATFORM_HINT=wayland" in start
    assert 'while [ "$display_tries" -lt 100 ]' in start
    assert '[ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]' in start
    assert "could not find Sway's Wayland display socket" in start
    assert "ulimit -S -c 0" in start
    assert "ulimit -c 0\n" not in start
    assert 'max_core = 0' in start
    assert '$HOME/.config/libvirt/qemu.conf' in start


def test_upgrade_removes_optioned_printscreen_bindings_before_adding_one_copy():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert "bindsym .*?(Print|Ctrl\\+Shift\\+s|Shift\\+Print)" in ebuild
    assert "outputs.conf" in ebuild
    assert "include ~/.config/sway/outputs.conf" in ebuild
    assert "floating_modifier $mod normal" in ebuild


def test_super_is_a_global_physical_key_binding_not_a_bare_modifier_binding():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    assert "bindsym --release --no-repeat Super_L exec swaymsg -t send_tick pc:start" in cfg
    assert "bindsym --release --no-repeat $mod exec swaymsg -t send_tick pc:start" not in cfg


def test_alt_tab_is_compositor_owned_and_migrated_to_existing_accounts():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    helper = ROOT / "os/bin/pc-window-cycle"
    assert "Mod1+Tab exec /usr/local/bin/pc-window-cycle next" in cfg
    assert "Mod1+Shift+Tab exec /usr/local/bin/pc-window-cycle previous" in cfg
    assert "pc-window-cycle" in ebuild
    assert helper.exists()


def test_native_windows_have_standard_close_shortcut_on_new_and_existing_accounts():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    installer = (ROOT / "os/gentoo.sh").read_text()
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert "bindsym Mod1+F4 kill" in cfg
    assert "bindsym Mod1+F4 kill" in installer
    assert "grep -qF 'Mod1+F4 kill'" in ebuild


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


def test_upgrade_restores_native_window_decorations_in_old_identity_configs():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert "default_floating_border[[:space:]]+none" in ebuild
    assert "default_floating_border normal 3" in ebuild
