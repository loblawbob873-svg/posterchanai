"""The Electron bridge that lets the page act as a desktop shell.

`desktop/wm.js` and `desktop/net.js` were written and tested first and then sat there, called by
nothing — which is a shape worth naming, because tested code that is not wired up looks finished
from every angle except the one that matters. This is the wiring, and what it must not do is more
interesting than what it does: `launch` starts a PROCESS, `connect` hands a wifi password to
NetworkManager, and `provision` runs a command as ROOT.

Electron cannot be run here (it needs a display), so this reads the two files and asserts the
properties that would otherwise only be discovered by someone with a screen and bad luck.
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "desktop", "main.js")
PRELOAD = os.path.join(ROOT, "desktop", "preload.js")


class Bridge(unittest.TestCase):
    def setUp(self):
        self.main = open(MAIN, encoding="utf-8").read()
        self.pre = open(PRELOAD, encoding="utf-8").read()

    def test_both_files_parse(self):
        for f in (MAIN, PRELOAD):
            r = subprocess.run(["node", "--check", f], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr[-600:])

    def test_the_modules_are_actually_called(self):
        """The gap this test exists to close: two tested modules that nothing invoked."""
        self.assertIn("./wm.js", self.main)
        self.assertIn("./net.js", self.main)
        for surface in ("pcWM", "pcNet", "pcOS", "pcPower", "pcAudio"):
            self.assertIn(surface, self.pre, f"{surface} is not exposed to the page")

    def test_pointer_gap_repair_cannot_abort_multi_monitor_startup(self):
        """A missing compositor variable must not skip reconcileShellDisplays and black an output."""
        repair = self.main.index("await displays().repairPointerGaps()")
        reconcile = self.main.index("await reconcileShellDisplays()", repair)
        block = self.main[repair - 120:reconcile]
        self.assertIn("try{", block)
        self.assertIn("catch(e)", block)
        self.assertIn("pointer-gap repair deferred", block)

    def test_every_privileged_handler_checks_the_sender(self):
        """`launch` starts a process and `connect` hands over a wifi password. A handler reachable
        from any page but our own is a remote code execution, and the check is one call — which is
        exactly the kind of thing that gets left off one handler out of twelve."""
        missing = []
        for m in re.finditer(r"ipcMain\.handle\('(pc:(?:wm|net|os|power|audio):[a-z]+)'\s*,\s*(async\s*)?\("
                             r"[^)]*\)\s*=>\s*\{?([^\n]*)", self.main):
            name, body = m.group(1), m.group(3)
            tail = self.main[m.end():m.end() + 400]
            if "fsGuard" not in body and "fsGuard" not in tail:
                missing.append(name)
        self.assertEqual(missing, [], f"handlers that do not check the sender: {missing}")

    def test_candidate_paths_are_resolved_against_the_filesystem(self):
        """Only this side can look. Gentoo installs firefox as /usr/bin/firefox-bin, not
        /usr/bin/firefox — a launcher in the page cannot know that, and a hardcoded path that does
        not exist starts nothing, silently, which is indistinguishable from a broken launcher."""
        i = self.main.index("'pc:wm:launch'")
        body = self.main[i:i + 1400]
        self.assertIn("accessSync", body, "candidates are not checked for existence")
        self.assertIn("not installed", body, "a program that is absent is not reported as absent")

    def test_launch_takes_an_argv_array_not_a_command_string(self):
        """A string would have to reach a shell to be useful, and then a file name with a space in
        it is an injection."""
        i = self.main.index("'pc:wm:launch'")
        body = self.main[i:i + 700]
        self.assertIn("Array.isArray", body, "a command string would be handed to a shell")
        self.assertNotIn("exec(", body)
        self.assertNotIn("shell: true", body)

    def test_provision_validates_the_npub_before_running_as_root(self):
        """It shells out to sudo. The page is not trusted to have checked its own input, and neither
        is the argument — the script checks it again on the other side."""
        i = self.main.index("'pc:os:provision'")
        body = self.main[i:i + 900]
        self.assertIn("npub1", body, "an unvalidated string is passed to a root command")
        self.assertIn("sudo", body)
        self.assertIn("-n", body, "sudo may not be allowed to prompt — it would hang the shell")

    def test_provision_runs_one_fixed_command(self):
        i = self.main.index("'pc:os:provision'")
        body = self.main[i:i + 900]
        self.assertIn("/usr/local/bin/pc-provision-user", body)
        self.assertIn("execFile", body, "a shell would make the argument executable")

    def test_a_launch_that_never_appears_is_not_reported_as_launched(self):
        i = self.main.index("'pc:wm:launch'")
        body = self.main[i:self.main.index("ipcMain.handle('pc:apps:list'", i)]
        self.assertIn("waitForWindow", body)

    def test_running_firefox_private_window_is_matched_by_new_surface_identity(self):
        i = self.main.index("'pc:wm:launch'")
        body = self.main[i:self.main.index("ipcMain.handle('pc:apps:list'", i)]
        self.assertIn("firefoxBefore", body)
        self.assertIn("waitForNewWindow(firefoxBefore", body)
        self.assertIn("/firefox/i.test(String(w.app||''))", body)

    def test_telegram_uses_the_working_xwayland_renderer_only_for_telegram(self):
        """Qt's Wayland EGL failure must not turn Telegram black or disable GPU use globally."""
        i = self.main.index("telegram-desktop|telegram-desktop-bin")
        body = self.main[i:i + 500]
        self.assertIn("QT_QPA_PLATFORM: 'xcb'", body)
        self.assertIn("DISPLAY: process.env.DISPLAY || ':0'", body)
        self.assertNotIn("process.env.QT_QPA_PLATFORM", self.main)

    def test_firefox_uses_native_wayland_pointer_geometry(self):
        i = self.main.index("firefox|firefox-bin")
        body = self.main[i:i + 1800]
        self.assertIn("GDK_BACKEND: 'wayland'", body)
        self.assertIn("MOZ_ENABLE_WAYLAND: '1'", body)
        self.assertIn("!firefoxRunning", body)

    def test_the_event_listener_can_be_removed(self):
        """The desktop redraws its taskbar on every window event; a listener the page cannot remove
        leaks a closure per view change."""
        i = self.pre.index("onEvent:")
        body = self.pre[i:i + 400]
        self.assertIn("removeListener", body)

    def test_ending_the_session_is_four_handlers_not_one_verb(self):
        """A single `pc:power:do(action)` is one typo away from a page asking to power off when it
        meant to sleep. Four names cannot be mistyped into each other."""
        for verb in ("suspend", "hibernate", "poweroff", "reboot"):
            self.assertIn(f"'pc:power:{verb}'", self.main, f"{verb} has no handler of its own")
        # The HANDLER, not the phrase — the comment above it explains why a verb argument would be
        # wrong, and a test that matches prose is a test about the comments.
        self.assertNotIn("ipcMain.handle('pc:power:do'", self.main)

    def test_the_modules_are_required_not_reimplemented(self):
        for mod in ("./power.js", "./audio.js"):
            self.assertIn(mod, self.main, f"{mod} is tested and not called")

    def test_shell_mode_shows_no_application_menu(self):
        """A menu bar reading File / Edit / View / Help across the top of an operating system is the
        single most convincing way to tell somebody they are looking at an app in a window. `null`
        rather than an empty template: an empty one still reserves the bar's height, which is a strip
        of nothing across the top of the screen that people will ask about."""
        self.assertIn("SHELL_MODE", self.main, "--shell is passed by the compositor and does nothing")
        i = self.main.index("function buildMenu")
        self.assertIn("Menu.setApplicationMenu(null)", self.main[i:i + 900],
                      "the shell still builds an application menu")

    def test_shell_mode_has_no_window_chrome(self):
        """The compositor decides the size, and it is the whole screen. A title bar, a resize border
        and remembered geometry are all statements that this is a window on a desktop."""
        i = self.main.index("new BrowserWindow")
        opts = self.main[i:i + 1400]
        self.assertIn("frame: !SHELL_MODE", opts)
        self.assertIn("autoHideMenuBar: SHELL_MODE", opts)
        self.assertNotIn("fullscreen: true,", opts)

    def test_shell_creates_and_scopes_one_surface_per_active_output(self):
        self.assertIn("require('./shell-displays.js')", self.main)
        self.assertIn("shellDisplays.plan(await wm().outputs()", self.main)
        self.assertIn("createWindow(assignment)", self.main)
        self.assertIn("_shellScopes.set(record.browser.webContents.id, assignment)", self.main)
        self.assertIn("'output'", self.main[self.main.index("const NAMES ="):])
        self.assertIn("--pc-secondary-surface", self.main)
        self.assertIn("backgroundOwner", self.pre)
        sync = open(os.path.join(ROOT, "static/js/client/sync.js"),
                    encoding="utf-8", errors="replace").read()
        self.assertIn("window.pcShell.backgroundOwner === false", sync,
                      "each monitor can start another folder-sync writer")

    def test_cold_boot_waits_for_wayland_to_map_the_primary_surface(self):
        self.assertIn("if(!own) own = await newShellContainer(rows)", self.main)
        self.assertIn("setTimeout(() => reconcileShellDisplays(), 1200)", self.main)

    def test_moving_a_shell_surface_reconciles_before_a_monitor_stays_black(self):
        events = self.main[self.main.index("const NAMES = ['window'"):]
        events = events[:events.index("let window = null")]
        self.assertIn("appId==='place.poster.desktop'", events)
        self.assertIn("pid===process.pid", events)
        self.assertGreaterEqual(events.count("scheduleDisplayReconcile()"), 2)
        reconcile = self.main[self.main.index("async function reconcileShellDisplays()"):
                              self.main.index("function scheduleDisplayReconcile()")]
        self.assertIn("shellDisplays.needsPlacement(current,assignment)", reconcile)

    def test_a_shell_restart_recovers_which_monitor_owned_a_stashed_app(self):
        """Sway keeps scratchpad apps across an Electron restart; the in-memory owner map is lost."""
        self.assertIn("function ownerFromRect(row)", self.main)
        scoped = self.main[self.main.index("function scopedWindows(e, rows)"):]
        scoped = scoped[:scoped.index("let _shellRecoveryWired")]
        self.assertIn("ownerFromRect(row)", scoped)
        self.assertIn("_nativeOwners.set(id, owner)", scoped)

    def test_native_windows_can_be_handed_to_an_adjacent_display(self):
        self.assertIn("'pc:wm:handoff'", self.main)
        self.assertIn("handoff:", self.pre)
        self.assertIn("move container to workspace number", self.main)
        self.assertIn("finishMove(nativeId)", self.main)
        self.assertLess(self.main.index("pc:wm:native-handoff-prepare"),
                        self.main.index("finishMove(nativeId)"))
        self.assertIn("wm().place(nativeId,prepared.x,prepared.y,prepared.w,prepared.h)", self.main)
        self.assertIn("destination frame geometry is outside output", self.main)
        self.assertIn("await ops.rollback()", open(os.path.join(ROOT, "desktop/native-handoff.js"), encoding="utf-8").read())
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        self.assertIn("pcWM.handoff(id,handoff,", client)
        self.assertIn("killNative:false,preserveFocus:true", client)

    def test_posterchan_frames_can_be_handed_to_an_adjacent_display(self):
        self.assertIn("'pc:wm:handoff-frame'", self.main)
        self.assertIn("scrollTop:Math.max(0,Number(p.scrollTop)||0)", self.main)
        self.assertIn("handoffFrame:", self.pre)
        self.assertIn("onHandoffFrame:", self.pre)
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        self.assertIn("pcWM.handoffFrame(handoffPayload(w,overflow),direction)", client)
        self.assertIn("pcWM.onHandoffFrame", client)
        self.assertIn("'pc:wm:preview-frame'", self.main)
        self.assertIn("previewFrame:", self.pre)
        self.assertIn("onPreviewFrame:", self.pre)
        self.assertIn("pcWM.onPreviewFrame", client)
        self.assertIn("closeWin(w,{preserveFocus:true})", client)
        self.assertIn("state:", client, "the destination redraws module-local apps from scratch")
        self.assertIn("PCWebSearch.handoffState()", client)
        self.assertIn("PCWebSearch.acceptHandoff(p.state)", client)
        self.assertIn("ui:captureHandoffUI(w)", client)
        self.assertIn("restoreHandoffUI(w,p.ui)", client)
        self.assertNotIn('data-w="monitor"', client)
        self.assertIn("terminalSid:terminal", client)

    def test_every_posterchan_app_uses_the_generic_state_preserving_handoff(self):
        """There must be no view whitelist: every sidebar app, including ones added later, carries
        forms, selections, scroll positions and media state through the same payload."""
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        html = open(os.path.join(ROOT, "templates/client.html"), encoding="utf-8").read()
        views = set(re.findall(r'data-view=["\']([^"\']+)', html))
        self.assertGreater(len(views), 20, "the exhaustive app matrix did not find the real sidebar")
        start = client.index("function handoffPayload(")
        payload = client[start:client.index("function sendFrameHandoff", start)]
        receive = client[client.index("pcWM.onHandoffFrame"):client.index("pcWM.onPreviewFrame", client.index("pcWM.onHandoffFrame"))]
        self.assertIn("ui:captureHandoffUI(w)", payload)
        self.assertIn("restoreHandoffUI(w,p.ui)", receive)
        for view in views:
            self.assertNotIn("p.view==='" + view + "'&&p.ui", receive,
                             view + " fell back to a one-off handoff instead of the generic path")
        # reconstructHandoffWindow performs the destination's one render. A second focusWin used
        # to reload all apps.
        after_open = receive[receive.index("const w=reconstructHandoffWindow"):]
        self.assertNotIn("focusWin(w);", after_open)

    def test_every_registered_app_keeps_its_opened_identity_across_outputs(self):
        """The registry is the matrix: adding an app must automatically put it under this gate.

        A monitor's page-global VIEW is never an application identity. Ordinary sidebar apps are
        reconstructed from the transferred opened view; only the explicitly asserted aliases may
        map to another reconstructible identity.
        """
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        html = open(os.path.join(ROOT, "templates/client.html"), encoding="utf-8").read()
        views = sorted(set(re.findall(r'data-view=["\']([^"\']+)', html)))
        self.assertGreater(len(views), 20)
        identity = client[client.index("function handoffIdentity("):
                          client.index("function selectedMessagesTab(")]
        receive = client[client.index("function reconstructHandoffWindow("):
                         client.index("function selectedMessagesTab(")]
        self.assertIn("return openApp(view", receive,
                      "registered apps no longer use the transferred identity")
        self.assertNotIn("PC().VIEW", receive,
                         "destination-global state must not choose the received application")
        for view in views:
            # No registered app gets a receiver-side one-off mapping. Such mappings are where
            # Profile became Terminal and Terminal became Social in prior releases.
            self.assertNotIn("view==='" + view + "'", receive, view)
        self.assertIn("return opened||current", identity)
        for opened, expected in (("terminal", "terminal"), ("websearch", "websearch"),
                                 ("messages", "messages"), ("concord", "messages"),
                                 ("doc:music", "__music"),
                                 ("doc:os-settings", "__ossettings")):
            self.assertIn("opened==='" + opened + "'", identity)
            self.assertIn("return '" + expected + "'", identity)

    def test_profile_and_post_documents_reconstruct_their_exact_content(self):
        """Opaque document keys may never fall through to openApp's shared-feed fallback."""
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        classify = client[client.index("function handoffDocumentKind("):
                          client.index("function reconstructHandoffWindow(")]
        rebuild = client[client.index("function reconstructHandoffWindow("):
                         client.index("function selectedMessagesTab(")]
        self.assertIn("/^doc:prof:[0-9a-f]{64}$/i", classify)
        self.assertIn("/^doc:post:[0-9a-f]{64}$/i", classify)
        self.assertIn("PC().openProfile(pk)", rebuild)
        self.assertIn("PC().openThread(id)", rebuild)
        self.assertIn("wins.find(w=>w.view===view)", rebuild)
        self.assertIn("kind==='unsupported'", rebuild)
        send = client[client.index("function sendFrameHandoff("):
                      client.index("function rearmFrameHandoffDestination(")]
        self.assertIn("handoffDocumentKind(String(w.view||''))==='unsupported'", send)
        self.assertIn("return Promise.resolve(false)", send,
                      "unknown documents must remain on their source output")

    def test_video_handoff_preserves_playback_instead_of_reopening_black(self):
        """Moving a playing video across outputs recreates DOM in another renderer.

        The route reconstructs the same post; the generic snapshot must then restore the exact
        media element by id/index and resume only when it was playing. This protects videos in
        Social, Concord attachments and Webxdc without a fragile per-app exception.
        """
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        capture = client[client.index("function captureHandoffUI("):
                         client.index("function restoreHandoffUI(")]
        restore = client[client.index("function restoreHandoffUI("):
                         client.index("function handoffIdentity(")]
        self.assertIn("querySelectorAll('audio,video')", capture)
        for field in ("time:el.currentTime", "paused:!!el.paused", "volume:el.volume",
                      "muted:!!el.muted", "rate:el.playbackRate"):
            self.assertIn(field, capture)
        self.assertIn("find(root,x,'audio,video')", restore)
        self.assertIn("el.currentTime=x.time", restore)
        self.assertIn("if(!x.paused)el.play().catch", restore)
        self.assertIn("++tries<30", restore,
                      "async attachment/video rendering needs retries after the destination paints")

    def test_native_apps_receive_real_decorations(self):
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        self.assertIn("pc:wm:decorate", self.main)
        self.assertIn("decorate:", self.pre)
        self.assertIn("pcWM.decorate(id)", client)

    def test_native_taskbar_has_a_close_action(self):
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        self.assertIn("label:'Close'", client)
        self.assertIn("pcWM.close(w.id)", client)

    def test_native_taskbar_has_visible_window_controls(self):
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        # Inline controls removed by request; the right-click menu carries them now.
        self.assertIn('data-kind="native"', client)
        self.assertIn("'Maximize'", client)
        self.assertIn("'Close'", client)
        self.assertNotIn("if(w.native == null && window.pcWM && nativeTasks.length)", client)

    def test_terminal_handoff_keeps_the_same_pty(self):
        client = open(os.path.join(ROOT, "static/js/client/os.js"), encoding="utf-8").read()
        term = open(os.path.join(ROOT, "static/js/client/term.js"), encoding="utf-8").read()
        self.assertIn("terminalSid:terminal", client)
        self.assertIn("PCTerm.adoptSession(p.terminalSid)", client)
        self.assertIn("function adoptSession(id)", term)

    def test_global_shell_keys_only_reach_the_focused_monitor(self):
        self.assertIn("(await wm().workspaces()).find(x=>x && x.focused)", self.main)
        self.assertIn("(await wm().outputs()).find(x=>x&&x.focused)", self.main)
        self.assertIn("payload==='pc:start:close'", self.main)

    def test_global_shell_keys_are_forwarded_by_the_always_on_main_subscription(self):
        recovery = self.main[self.main.index("async function wireShellRecovery"):
                             self.main.index("async function reconcileShellDisplays")]
        self.assertIn("forwardShellTick(ev)", recovery)
        handler = self.main[self.main.index("ipcMain.handle('pc:wm:subscribe'"):
                            self.main.index("/* Power, brightness", self.main.index("ipcMain.handle('pc:wm:subscribe'"))]
        self.assertIn("const NAMES = ['window', 'workspace', 'output', 'tick']", handler)
        # This loop must not register a SECOND tick listener on the same socket. It used to, so
        # every desktop key press reached the renderer twice — measured on hardware with a probe
        # listener: {"pc:probe-one":2,"pc:probe-two":2}. `forwardShellTick` owns the channel and
        # does the focused-output scoping; `subscribe` still NAMES tick because the socket's list
        # is fixed on first subscription.
        self.assertIn("if (name === 'tick') continue;", handler,
                      "Super shortcuts must be scoped to the focused output, not delivered twice")
        self.assertNotIn("w.on('tick'", handler)

    def test_it_is_absent_rather_than_broken_without_a_compositor(self):
        """A desktop install that is not PosterChanOS has no sway. The page must be able to ask,
        rather than discovering it through a thrown error on every call."""
        self.assertIn("pc:wm:available", self.main)
        self.assertIn("available:", self.pre)

    def test_the_app_scan_is_wired_and_starts_nothing_itself(self):
        """The scan LISTS what is installed; starting one goes through `pcWM.launch`, which is the
        guarded path the built-in entries already use. Two ways to start a process is one more than
        anything needs, and the second is the one that gets the guard wrong."""
        self.assertIn("pc:apps:list", self.main, "nothing lists the machine's applications")
        self.assertIn("pcApps", self.pre, "the scan is not exposed to the page")
        # Guarded like everything else that reads this disk.
        m = re.search(r"ipcMain\.handle\('pc:apps:list'.*?\n\}\);", self.main, re.S)
        self.assertTrue(m, "the handler could not be found")
        self.assertIn("fsGuard(e)", m.group(0),
                      "the app scan is reachable from any page — it reads this machine's disk")
        # …and it must not grow a launcher of its own.
        self.assertNotIn("spawn(", m.group(0),
                         "the app list starts processes itself instead of going through pcWM.launch")

    def test_a_terminal_app_is_run_INSIDE_a_terminal(self):
        """`Terminal=true` (btop, nvim, an installer script) means the program has no window of its
        own. Started directly it is a process with its output attached to nothing, and nothing at
        all appears on screen — which reads as a launcher that does not work."""
        self.assertIn("terminalPrefix", self.main,
                      "a Terminal=true entry would be started with no terminal around it")
        m = re.search(r"ipcMain\.handle\('pc:apps:list'.*?\n\}\);", self.main, re.S)
        self.assertIn("a.terminal", m.group(0))
        # An entry nothing on this machine could run must not be offered — a dead button is worse
        # than an absence, because it looks like the program is broken rather than missing.
        self.assertIn("if (a.terminal && !term) continue", m.group(0),
                      "a terminal app is offered on a machine with no terminal to run it in")

    def test_the_compositor_can_talk_BACK_to_the_shell(self):
        """A sway binding can only run a command — it cannot call into this app. So anything the
        keyboard has to reach the desktop with goes out as a `tick`, which sway broadcasts to every
        IPC subscriber. That is what makes the Super key open the start menu while FIREFOX has the
        keyboard, and that is the only case that matters: a key handler in the page fires only when
        the page is focused, and you press Super to leave whatever you are in.

        Subscribing to `tick` is not enough on its own — the PAYLOAD is the message, and forwarding
        the event without it delivers a knock with nobody at the door."""
        self.assertIn("'tick'", self.main,
                      "the shell does not subscribe to tick, so no key binding can reach it")
        m = re.search(r"send\('pc:wm:event',\s*\{([^}]*)\}", self.main, re.S)
        self.assertTrue(m, "the wm event forward could not be found")
        self.assertIn("payload", m.group(1),
                      "a tick is forwarded without its payload — every binding looks the same")

    def test_a_new_window_is_forwarded_without_another_tree_walk(self):
        """The event already carries sway's container. Dropping it makes a new app appear first and
        its PosterChan frame catch up after two GET_TREE round trips."""
        i = self.main.index("if (name === 'window' && ev && ev.container)")
        body = self.main[i:i + 2200]
        self.assertIn("flatten(ev.container", body)
        self.assertIn("payload: ev && ev.payload, window", body)

    def test_windows_and_mac_get_a_REJECTION_not_an_empty_window_list(self):
        """THE ONE LINK THAT MAKES THE WINDOWS GUARD HOLD, and it is a single word wide.

        `PCOSShell.detect()` decides whether this machine is PosterChanOS with
        `Array.isArray(await pcWM.windows())` — and an EMPTY ARRAY IS AN ARRAY. So if the bridge
        answered a machine with no compositor by resolving `[]` ("no windows"), the shell would
        declare itself present on Windows and macOS and paint its wifi, volume, brightness and power
        tray onto somebody's ordinary desktop app. Asked for directly: "make sure the windows and
        mac versions don't have the PosterChanOS toolbars you made like for wifi, power".

        Windows and macOS never set SWAYSOCK or I3SOCK, so this is the case they take. It is RUN,
        with the environment cleared, rather than read: the difference between rejecting and
        resolving empty is invisible in the source and total on screen.
        """
        wm = os.path.join(ROOT, "desktop", "wm.js")
        src = (
            "const { WM } = require(%r);\n"
            "const w = new WM('');\n"
            "w.windows().then(\n"
            "  v => { console.log('RESOLVED ' + JSON.stringify(v)); },\n"
            "  e => { console.log('REJECTED ' + String(e && e.message)); });\n" % wm)
        env = dict(os.environ)
        env.pop("SWAYSOCK", None)
        env.pop("I3SOCK", None)
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True,
                           timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        self.assertTrue(r.stdout.startswith("REJECTED"),
                        "a machine with no compositor answered %r — detect() reads that as an "
                        "array and the OS shell draws itself on Windows" % r.stdout.strip())
        # And `available()` says so without a round trip, which is what the page asks first.
        r2 = subprocess.run(
            ["node", "-e", "const {WM}=require(%r); console.log(String(new WM('').available()));" % wm],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r2.stdout.strip(), "false", r2.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
