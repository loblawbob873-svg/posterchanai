"""`./install.sh --nostr-only` and `./install.sh --ai` — the installer run with NO TERMINAL, the way
PosterChanOS System Settings runs it through /usr/local/bin/pc-server.

RUN under bash in a scratch copy of the installer with every system command stubbed (sudo rewrites
/etc and /var into the scratch root; python3.13 builds a fake venv whose pip only logs). stdin is
/dev/null throughout, which is the whole point: under `set -e` a `read` that meets end-of-input returns 1
and ends the install at its first question with nothing said about why — so the pre-change installer
cannot complete a single one of these runs.

The interactive installer is driven too, with answers on stdin, to pin that it still ASKS: the
non-interactive mode must be an addition, never a change to what a person at a terminal gets.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REAL = ["bash", "sh", "cat", "grep", "sed", "awk", "tr", "mkdir", "chmod", "tee", "rm", "env", "sort", "head",
        "tail", "touch", "true", "false", "cp", "ls", "readlink", "date", "dirname", "basename", "mv", "install",
        "whoami", "id", "getent", "cut", "uname", "mktemp", "tar", "sleep", "find", "wc", "stat", "ln", "cmp",
        "printf", "echo", "test", "xargs", "python3", "b2sum", "sha256sum", "sha512sum", "od", "expr", "seq"]

STUBS = {
    # sudo: drop -n / -u USER, and move absolute /etc and /var paths into the scratch root.
    "sudo": '''args=()
while [ $# -gt 0 ]; do case "$1" in -n) shift;; -u) echo "sudo-u $2" >> "$STUB_LOG"; shift 2;; *) break;; esac; done
for a in "$@"; do case "$a" in /etc/*|/var/*) args+=("$STUB_ROOT$a");; *) args+=("$a");; esac; done
echo "sudo ${args[*]}" >> "$STUB_LOG"
exec "${args[@]}"''',
    "chown": 'echo "chown $*" >> "$STUB_LOG"',
    "systemctl": 'echo "systemctl $*" >> "$STUB_LOG"; exit 0',
    "timedatectl": 'echo "timedatectl $*" >> "$STUB_LOG"; exit 0',
    "psql": 'echo "psql $*" >> "$STUB_LOG"; exit 0',
    "gcc": 'exit 0', "cmake": 'exit 0', "pip3": 'exit 0',
    "git": 'echo "git $*" >> "$STUB_LOG"; exit 1',
    "curl": 'echo "curl $*" >> "$STUB_LOG"; exit 22',
    "wget": 'echo "wget $*" >> "$STUB_LOG"; exit 1',
    "sqlite3": 'exit 1',
    # The interpreter install.sh picks first. `-m venv DIR` makes a venv whose python/pip only log.
    "python3.13": r'''if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  d="$3"; mkdir -p "$d/bin"
  printf '#!/bin/bash\necho "venv-python $*" >> "$STUB_LOG"\nexit 0\n' > "$d/bin/python"
  printf '#!/bin/bash\necho "pip $*" >> "$STUB_LOG"\n[ "$1" = show ] && exit 1\nexit 0\n' > "$d/bin/pip"
  chmod +x "$d/bin/python" "$d/bin/pip"; ln -sf python "$d/bin/python3"
  printf 'export PATH="%s/bin:$PATH"\ndeactivate(){ :; }\n' "$(cd "$d" && pwd)" > "$d/bin/activate"
  exit 0
fi
[ "$1" = "--version" ] && { echo "Python 3.13.9"; exit 0; }
exit 0''',
}


class Checkout:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.src = tmp / "posterchanai"
        (self.src / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "install.sh", self.src / "install.sh")
        shutil.copytree(ROOT / "scripts" / "install", self.src / "scripts" / "install")
        for f in ("requirements-nostr.txt", "requirements.txt", "media-center.env.example"):
            shutil.copy2(ROOT / f, self.src / f)
        (self.src / "scripts" / "init_instance_key.py").write_text("")
        self.root = tmp / "root"
        (self.root / "etc" / "systemd" / "system").mkdir(parents=True)
        (self.root / "var" / "lib").mkdir(parents=True)
        self.bin, self.real = tmp / "bin", tmp / "real"
        self.bin.mkdir()
        self.real.mkdir()
        for t in REAL:
            p = shutil.which(t)
            if p:
                (self.real / t).symlink_to(p)
        for name, body in STUBS.items():
            (self.bin / name).write_text("#!/bin/bash\n" + body + "\n")
            (self.bin / name).chmod(0o755)
        self.log = tmp / "calls.log"

    def run(self, *args, stdin=None, **extra):
        self.log.write_text("")
        env = {"PATH": f"{self.bin}:{self.real}", "HOME": str(self.tmp), "STUB_LOG": str(self.log),
               "STUB_ROOT": str(self.root), "POSTERCHANAI_DATA": str(self.tmp / "data-root"), **extra}
        p = subprocess.run(["bash", str(self.src / "install.sh"), *args], cwd=self.src, env=env,
                           input=stdin if stdin is not None else "", capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout + p.stderr, self.log.read_text().splitlines()

    @property
    def unit(self):
        return (self.root / "etc/systemd/system/posterchanai.service").read_text()

    @property
    def secrets(self):
        return (self.src / "data" / "secrets.env").read_text()


@pytest.fixture
def co(tmp_path):
    return Checkout(tmp_path)


def test_nostr_only_completes_with_no_terminal_and_writes_a_unit_for_the_named_account(co):
    rc, out, calls = co.run("--nostr-only", "--service-user", "nobody", "--no-start")
    assert rc == 0, out[-3000:]
    assert "[non-interactive]" in out
    assert "pip install -r requirements-nostr.txt -q" in calls, "the Nostr-only venv is the lean one"
    assert not [c for c in calls if c.startswith("pip install -r requirements.txt")], "no AI stack"
    assert "User=nobody" in co.unit, "PosterChanOS runs the installer as root; the unit must not be"
    assert "export POSTERCHANAI_NOSTR_ONLY=1" in co.secrets and "export POSTERCHANAI_NOSTR_RELAY=1" in co.secrets
    assert "systemctl daemon-reload" in " ".join(calls)
    assert not [c for c in calls if " enable " in f" {c} " or c.endswith(" start posterchanai")], \
        "--no-start: the caller decides when it runs"
    assert any("CREATE DATABASE posterchan_relay" in c for c in calls)
    # SearXNG is part of the NORMAL install on this path too, as on the Full one (Step 9d2).
    src = (ROOT / "install.sh").read_text()
    body = src[src.index("install_nostr_only() {"):src.index("\n}\n", src.index("install_nostr_only() {"))]
    assert "setup_searxng ||" in body, "the nostr-only install must install SearXNG, non-fatally"


def test_nostr_only_starts_the_service_by_default(co):
    rc, out, calls = co.run("--nostr-only")
    assert rc == 0, out[-3000:]
    assert "systemctl enable posterchanai" in " | ".join(calls)
    assert f"User={os.environ.get('USER') or subprocess.run(['whoami'], capture_output=True, text=True).stdout.strip()}" in co.unit


@pytest.mark.parametrize("args", [("--nostr-only", "--service-user"), ("--nostr-only", "--bogus"),
                                  ("--ai", "--backend", "tpu"), ("--nostr-only", "--service-user", "no-such-user-x")])
def test_bad_flags_stop_before_anything_is_installed(co, args):
    rc, out, calls = co.run(*args)
    assert rc == 2, out[-2000:]
    assert calls == []


def test_ai_is_the_full_install_with_every_question_defaulted_and_unhides_the_ai(co):
    # A node that started Nostr-only…
    assert co.run("--nostr-only", "--no-start")[0] == 0
    rc, out, calls = co.run("--ai", "--backend", "cpu", "--service-user", "nobody", "--no-start")
    assert rc == 0, out[-4000:]
    assert "pip install -r requirements.txt -q" in calls
    assert any(c.startswith("pip install llama-cpp-python") for c in calls), "the LLM half"
    assert "export POSTERCHANAI_NOSTR_ONLY=0" in co.secrets, \
        "a Full install that leaves NOSTR_ONLY=1 installs every AI dependency and then hides all of it"
    assert "User=nobody" in co.unit
    assert not [c for c in calls if c.startswith("systemctl enable")]
    # Defaults, not choices: no model download, no music/video add-ons, no webxdc vhost.
    assert not [c for c in calls if ".gguf" in c]
    assert not [c for c in calls if "ACE-Step" in c or "chatterbox" in c]
    assert "Installation Complete!" in out, "the run must reach the installer's own summary"


def test_the_interactive_installer_still_asks(co):
    """Answers on stdin: 2 = Nostr-only, '' = install the service (default Y), n = do not start it.
    If `ask` had become non-interactive for everybody, the 'n' would never be read and it would start."""
    rc, out, calls = co.run(stdin="2\n\nn\n")
    assert rc == 0, out[-3000:]
    assert "[non-interactive]" not in out
    assert "pip install -r requirements-nostr.txt -q" in calls
    assert not [c for c in calls if c.startswith("systemctl enable")], "the 'n' answer was not read"


def test_the_interactive_installer_with_no_terminal_dies_at_its_first_question(co):
    """The reason the flags exist, pinned: this is what a GUI got before them."""
    rc, out, calls = co.run(stdin="")
    assert rc != 0
    assert not (co.root / "etc/systemd/system/posterchanai.service").exists()


def test_every_prompt_in_the_installer_goes_through_ask():
    """A new `read -p` in any module the two flags run is a question the GUI's install cannot answer —
    it ends the whole install at that line. webxdc.sh is exempt: it only runs from --webxdc."""
    offenders = []
    for f in [ROOT / "install.sh", *sorted((ROOT / "scripts/install").glob("*.sh"))]:
        if f.name in ("webxdc.sh",):
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if ("read -p" in s or "read -r -p" in s) and 'read -p "$__prompt"' not in s:
                offenders.append(f"{f.name}:{n}: {s}")
    assert not offenders, offenders
