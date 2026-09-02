"""A popped-out PosterChan window has to BE a window — bridges, one view, full width.

Everything here was measured on the real two-monitor PosterChanOS machine (build 1.0.1382) with the
Chrome DevTools protocol attached to the shell, not reasoned about from the source.
"""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "tests/client/oswin_window_is_usable_sim.js"


def test_a_posterchan_window_is_a_usable_window():
    """Four rules, run against the shipped main.js / preload.js / os.js / client.css.

    Before them, `swaymsg` reported a correct floating `PosterChan Window — terminal` at 1100x760
    and the page inside it had NO bridges at all — `window.pcClip`, which preload.js exposes
    unconditionally on its first lines, was `undefined`. shell.log carried one line per window:

        TypeError: Cannot destructure property 'preloadScripts' of 'binding.startupData' as it is
        null.

    The window asked for `sandbox: false` while sharing the opener's sandboxed process. With no
    `pcTerm` the terminal view is `hidden gated-off` on an instance-less machine, and with no
    `PCOSShell` the window fell through to the remembered desktop preference and built a SECOND
    DESKTOP inside itself — whose `#os-root` `html.pc-oswin` hides, taking `#feed` (0x0, holding
    477KB of timeline HTML) with it. Photographed as a 1100x760 rectangle of background gradient.
    """
    run = subprocess.run(["node", str(SIM)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK a PosterChan window is a usable window" in run.stdout


def test_the_terminal_key_opens_a_local_shell_not_the_last_ssh_host():
    """`openTerminalHere()` arms the LOCAL PTY and then opens the app; the compositor's Alt+Return
    tick called `openApp('terminal')` directly, so the key opened the terminal app pointed at
    whatever host this device last SSH'd into. The function existed and had no callers at all."""
    client = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    assert "function openTerminalHere()" in client
    tick = client[client.index("else if(p === 'pc:terminal')"):]
    tick = tick[:tick.index("else if(p === 'pc:tasks')")]
    assert "openTerminalHere()" in tick, "the terminal key does not arm the local shell"
    assert "openApp('terminal')" not in tick, "two ways to open one terminal will drift"


TASK_SIM = ROOT / "tests/client/oswin_is_a_task_sim.js"


def test_a_posterchan_window_gets_a_taskbar_button_and_an_alt_tab_row():
    """`taskbarRows` skips our own app_id — it has to, a taskbar button for the DESKTOP is recursive
    — and a real popped-out window carries that same app_id, so it was swallowed with the desktop.

    Measured on the machine: `PosterChan Window — terminal` floating at 986,664 1100x760, the DP-1
    taskbar listing only Telegram, and `PCOS.__switchRows()` answering one row for somebody else's
    app. Frameless with no title bar of its own, so the window also had no close, minimise or
    maximise anywhere. The TITLE is the discriminator, and it is already what sway's float rule
    keys on.
    """
    run = subprocess.run(["node", str(TASK_SIM)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK a PosterChan window is a task" in run.stdout


def test_our_own_window_is_never_wrapped_in_a_hosted_frame():
    """Hosting draws a PosterChan frame around somebody ELSE'S application. Doing it to a PosterChan
    window wraps this client in a screenshot of itself."""
    client = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    host = client[client.index("if(_hostNative) for(const r of rows)"):]
    host = host[:host.index("\n")]
    assert "!r.own" in host
