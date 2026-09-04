"""Read the shipped `wayfire.ini` the way the compositor does, for tests that used to read
`sway.config`.

Sway's config was a list of `bindsym <chord> exec <command>` lines, so a dozen tests each grew their
own regex over it. Wayfire's is an INI where a binding is a PAIR -- `binding_x = <chord>` and
`command_x = <command>` -- and the pairing is exactly where the interesting faults live: a binding
with no command is a dead key, and two bindings on one chord means one of them silently loses. Parse
it once, here, so those tests assert on chords and commands rather than on a file format.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini"


def sections(text=None):
    """{section: {key: value}} — comments and blank lines dropped."""
    out, current = {}, None
    for raw in (text if text is not None else CONFIG.read_text(encoding="utf-8")).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line.strip("[]")
            out.setdefault(current, {})
            continue
        if "=" not in line or current is None:
            continue
        key, _, value = line.partition("=")
        out[current][key.strip()] = value.strip()
    return out


def bindings(text=None):
    """{chord: command} for every key binding, including the Super-release one.

    A chord is the literal Wayfire spelling (`<super> KEY_LEFT`, `<ctrl> <alt> KEY_DELETE`,
    `KEY_SYSRQ`). A command may be a `;`-separated chain: Wayfire runs it through `sh -c`, which is
    how one key both marks the Super modifier consumed and performs its real action.
    """
    command = sections(text).get("command", {})
    out = {}
    for key, chord in command.items():
        for prefix in ("binding_", "release_binding_"):
            if key.startswith(prefix):
                run = command.get("command_" + key[len(prefix):])
                if run is not None:
                    out[chord] = run
                break
    return out


def runs(action, text=None):
    """Every chord whose command mentions `action`. Empty means nothing is bound to it."""
    return sorted(chord for chord, cmd in bindings(text).items() if action in cmd)
