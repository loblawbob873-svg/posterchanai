"""os/bin/pc-server — PosterChanOS's privileged door to the bundled server, RUN under bash with every system
command stubbed.

The stubs keep state (enabled/active units, which users exist) in files, so a second `enable` sees what the
first one did. `systemd-run` runs the job SYNCHRONOUSLY with the environment the real one would give it, so the
whole enable → installer → unit chain is observable from one call. The Box has PostgreSQL INSTALLED (two slots),
because the helper must leave a machine's PostgreSQL alone (#161): there is no database to set up any more.
tests/test_fresh_install_has_no_postgres.py runs the same harness with every Postgres tool stubbed.

What matters most here is what the helper REFUSES: it is reachable through a NOPASSWD sudo rule by every
administrator's desktop, so an unknown verb, an unlisted feature, `__run` from outside a job and a mutating
verb without root must all stop before anything is called.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "os" / "bin" / "pc-server"
REAL = ["bash", "sh", "cat", "grep", "sed", "tr", "mkdir", "chmod", "tee", "rm", "env", "sort", "head", "tail",
        "touch", "true", "false", "cp", "ls", "readlink", "date", "seq", "printf", "dirname", "basename", "mv"]

STUBS = {
    "id": '''if [ "$1" = "-u" ] && [ $# -eq 1 ]; then echo "${STUB_UID:-0}"; exit 0; fi
if [ "$1" = "-u" ]; then grep -qx "$2" "$STUB_STATE/users" 2>/dev/null; exit; fi
echo "uid=0(root)"''',
    "useradd": 'echo "useradd $*" >> "$STUB_LOG"; echo "${@: -1}" >> "$STUB_STATE/users"',
    "chown": 'echo "chown $*" >> "$STUB_LOG"',
    "sleep": 'exit 0',
    "journalctl": 'echo "journalctl $*" >> "$STUB_LOG"; echo "a log line"',
    "systemctl": '''echo "systemctl $*" >> "$STUB_LOG"
verb="$1"; shift
[ "$1" = "--now" ] && { now=1; shift; }
u="$1"
case "$verb" in
  is-active) [ -f "$STUB_STATE/active-$u" ] && echo active || { echo inactive; exit 3; } ;;
  is-enabled) [ -f "$STUB_STATE/enabled-$u" ] && echo enabled || { echo disabled; exit 1; } ;;
  enable) touch "$STUB_STATE/enabled-$u"; [ -n "${now:-}" ] && touch "$STUB_STATE/active-$u" ;;
  disable) rm -f "$STUB_STATE/enabled-$u"; [ -n "${now:-}" ] && rm -f "$STUB_STATE/active-$u" ;;
  restart) touch "$STUB_STATE/active-$u" ;;
esac
exit 0''',
    # The real one returns at once and runs the command as a unit; running it inline keeps the test
    # deterministic while giving the job exactly the environment systemd would (the --setenv).
    "systemd-run": '''echo "systemd-run $*" >> "$STUB_LOG"
envs=()
while [ $# -gt 0 ]; do case "$1" in --setenv=*) envs+=("${1#--setenv=}"); shift;; --*) shift;; *) break;; esac; done
env "${envs[@]}" INVOCATION_ID=stub "$@" >/dev/null 2>&1 || true''',
    "emerge": '''echo "emerge $*" >> "$STUB_LOG"
slot="${@: -1}"; slot="${slot##*:}"
mkdir -p "$STUB_PGDATA/$slot/data" "$STUB_PGETC/postgresql-$slot"
echo PG_VERSION > "$STUB_PGDATA/$slot/data/PG_VERSION"
echo "local all all trust" > "$STUB_PGETC/postgresql-$slot/pg_hba.conf"''',
    "runuser": 'echo "runuser $*" >> "$STUB_LOG"; exit 0',
}

FAKE_INSTALL = r'''#!/bin/bash
echo "install.sh $* | dsn=${DATABASE_URL:-}${NOSTR_RELAY_PG_DSN:-} HOME=$HOME ACESTEP_DIR=$ACESTEP_DIR" >> "$STUB_LOG"
[ -n "${STUB_INSTALL_FAIL:-}" ] && exit 1
case "$1" in
  --nostr-only) mkdir -p venv/bin venv/lib/python3.13/site-packages; touch venv/bin/python; chmod +x venv/bin/python
                echo "[Unit]" > "$PC_SERVER_UNIT_DIR/posterchanai.service"
                mkdir -p data; echo "export POSTERCHANAI_NOSTR_ONLY=1" >> data/secrets.env
                [ -n "${STUB_OLD_INSTALLER:-}" ] || { mkdir -p venv/lib/python3.13/site-packages/searxng-2026.9.dist-info
                  echo "[Unit]" > "$PC_SERVER_UNIT_DIR/posterchanai-searxng.service"; } ;;
  --searxng) mkdir -p venv/lib/python3.13/site-packages/searxng-2026.9.dist-info
             echo "[Unit]" > "$PC_SERVER_UNIT_DIR/posterchanai-searxng.service" ;;
  --ai) mkdir -p venv/lib/python3.13/site-packages/torch-2.12.dist-info venv/lib/python3.13/site-packages/llama_cpp_python-0.3.dist-info ;;
  --music) mkdir -p venv/lib/python3.13/site-packages/acestep-1.5.dist-info ;;
esac
exit 0
'''


class Box:
    def __init__(self, tmp: Path, *, pg=True):
        self.tmp = tmp
        self.bin, self.real, self.state = tmp / "bin", tmp / "real", tmp / "stubstate"
        for d in (self.bin, self.real, self.state):
            d.mkdir(parents=True, exist_ok=True)
        for t in REAL:
            p = shutil.which(t)
            if p:
                (self.real / t).symlink_to(p)
        for name, body in STUBS.items():
            (self.bin / name).write_text("#!/bin/bash\n" + body + "\n")
            (self.bin / name).chmod(0o755)
        self.app = tmp / "opt" / "posterchan-server"
        self.app.mkdir(parents=True)
        (self.app / "install.sh").write_text(FAKE_INSTALL)
        (self.app / "install.sh").chmod(0o755)
        self.home = tmp / "var" / "lib" / "posterchan-server"
        self.units = tmp / "etc" / "systemd" / "system"
        self.units.mkdir(parents=True)
        self.pglib = tmp / "usr" / "lib64"
        if pg:
            for slot in ("17", "18"):
                initdb = self.pglib / f"postgresql-{slot}" / "bin" / "initdb"
                initdb.parent.mkdir(parents=True)
                initdb.write_text("#!/bin/sh\n")
                initdb.chmod(0o755)
        self.pgdata, self.pgetc = tmp / "var" / "lib" / "postgresql", tmp / "etc"
        self.log = tmp / "calls.log"
        self.log.write_text("")

    def run(self, *args, **extra):
        self.log.write_text("")
        env = {"PATH": f"{self.bin}:{self.real}", "HOME": str(self.tmp), "STUB_LOG": str(self.log),
               "STUB_STATE": str(self.state), "STUB_PGDATA": str(self.pgdata), "STUB_PGETC": str(self.pgetc),
               "PC_SERVER_APP": str(self.app), "PC_SERVER_STATE": str(self.home),
               "PC_SERVER_UNIT_DIR": str(self.units), "PC_PG_LIBDIR": str(self.pglib),
               "PC_PG_DATA_ROOT": str(self.pgdata), "PC_PG_ETC_ROOT": str(self.pgetc),
               "PC_SERVER_PIP_CACHE": str(self.tmp / "pipcache"), **extra}
        p = subprocess.run([shutil.which("bash"), str(HELPER), *args], env=env, capture_output=True, text=True,
                           timeout=60)
        return p.returncode, p.stdout + p.stderr, self.log.read_text().splitlines()

    def status(self):
        rc, out, _ = self.run("status")
        assert rc == 0, out
        return json.loads(out)

    def job_log(self):
        return (self.home / "job.log").read_text()


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


# ---------------------------------------------------------------------------------------- refusals
@pytest.mark.parametrize("args", [(), ("frobnicate",), ("install-ai",), ("install-ai", "rm -rf /"),
                                  ("install-ai", "ai;id"), ("install-ai", "office"), ("enable", "extra"),
                                  ("logs", "-f")])
def test_anything_not_on_the_list_is_refused_before_it_runs_anything(box, args):
    rc, out, calls = box.run(*args)
    assert rc == 2, out
    assert calls == [], f"refused input still ran {calls}"


def test_the_internal_job_verb_is_not_reachable_from_outside_a_job(box):
    rc, out, calls = box.run("__run", "enable")
    assert rc != 0 and "internal" in out
    assert calls == []


@pytest.mark.parametrize("verb", ["enable", "disable", "restart", "logs"])
def test_mutating_and_journal_verbs_need_root(box, verb):
    rc, out, calls = box.run(verb, STUB_UID="1001")
    assert rc != 0 and "root" in out, out
    assert not [c for c in calls if c.startswith(("systemctl", "systemd-run", "journalctl"))]


# ---------------------------------------------------------------------------------------- status
def test_status_before_anything_is_set_up_says_so_rather_than_stopped(box):
    s = box.status()
    assert s["code"] is True and s["configured"] is False and s["venv"] is False
    assert s["port"] == 3051 and s["relayPort"] == 3052
    assert "postgres" not in s and s["database"] == "posterchandb", "no database server is part of the server"
    assert s["features"] == {f: False for f in ("ai", "music", "video", "voice", "searxng")}
    assert s["job"]["running"] is False


# ---------------------------------------------------------------------------------------- enable
def test_enable_runs_the_installer_and_starts_the_unit(box):
    rc, out, calls = box.run("enable")
    assert rc == 0, out
    log = box.job_log()
    assert "PosterChan server enabled" in log, log
    text = "\n".join(calls)
    assert "useradd --system" in text, "the service account must exist before anything is chowned to it"
    inst = next(c for c in calls if c.startswith("install.sh"))
    assert "install.sh --nostr-only --service-user posterchan-server --no-start" in inst
    assert "dsn= " in inst, "the installer is handed no database (#161)"
    assert "systemctl enable --now posterchanai.service" in text
    assert "postgresql" not in text and "emerge --config" not in text, "the machine's PostgreSQL is not ours"
    secrets = (box.app / "data" / "secrets.env").read_text()
    assert "DATABASE_URL" not in secrets and "NOSTR_RELAY_PG_DSN" not in secrets, secrets
    assert f"chown -R posterchan-server:posterchan-server {box.app}" in text
    s = box.status()
    assert s["configured"] and s["venv"] and s["enabled"] == "enabled" and s["active"] == "active"
    assert s["nostrOnly"] is True and s["job"]["rc"] == "0"


def test_a_second_enable_does_not_reinstall(box):
    assert box.run("enable")[0] == 0
    box.run("disable")
    rc, out, calls = box.run("enable")
    assert rc == 0, out
    text = "\n".join(calls)
    assert not any(c.startswith("install.sh") for c in calls), "an installed server is only started"
    assert "systemctl enable --now posterchanai.service" in text


def test_a_cluster_that_is_already_there_is_left_alone(box):
    data = box.pgdata / "18" / "data"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("18")
    etc = box.pgetc / "postgresql-18"
    etc.mkdir(parents=True)
    (etc / "pg_hba.conf").write_text("somebody's own rules\n")
    rc, out, calls = box.run("enable")
    assert rc == 0, out
    assert not [c for c in calls if "postgresql" in c or c.startswith("emerge --config")], calls
    assert (etc / "pg_hba.conf").read_text() == "somebody's own rules\n"
    assert "DATABASE_URL" not in (box.app / "data" / "secrets.env").read_text()
    # …and turning the server off must not take somebody else's database down with it.
    _, _, calls = box.run("disable")
    assert not [c for c in calls if "postgresql" in c]


def test_enable_on_a_machine_without_postgres_just_works(tmp_path):
    """It used to stop here with "PostgreSQL is not installed"; a fresh PosterChanOS ships none (#161)."""
    b = Box(tmp_path, pg=False)
    rc, out, calls = b.run("enable")
    assert rc == 0, out
    assert "PosterChan server enabled" in b.job_log(), b.job_log()
    assert "rc=0" in (b.home / "job.state").read_text()
    assert any(c.startswith("install.sh --nostr-only") for c in calls)


def test_a_failed_installer_is_reported_and_nothing_is_started(box):
    rc, out, calls = box.run("enable", STUB_INSTALL_FAIL="1")
    assert "the installer failed" in box.job_log()
    assert "systemctl enable --now posterchanai.service" not in "\n".join(calls)
    assert box.status()["job"]["rc"] == "1"


def test_a_second_job_is_refused_while_one_runs(box):
    (box.state / "active-posterchan-server-job").write_text("")
    rc, out, calls = box.run("enable")
    assert rc != 0 and "still running" in out
    assert not [c for c in calls if c.startswith("systemd-run")]


# ---------------------------------------------------------------------------------------- AI
def test_ai_features_need_the_server_and_the_torch_stack_first(box):
    rc, out, _ = box.run("install-ai", "ai")
    assert rc != 0 and "enable the server first" in out
    box.run("enable")
    rc, out, calls = box.run("install-ai", "music")
    assert rc != 0 and "AI chat + images" in out
    assert not [c for c in calls if c.startswith("systemd-run")]


def test_install_ai_runs_the_installers_own_paths_and_restarts_a_running_server(box):
    box.run("enable")
    rc, out, calls = box.run("install-ai", "ai")
    assert rc == 0, out
    inst = next(c for c in calls if c.startswith("install.sh"))
    assert inst.startswith("install.sh --ai --service-user posterchan-server --no-start")
    assert "systemctl restart posterchanai.service" in calls
    assert box.status()["features"]["ai"] is True
    rc, out, calls = box.run("install-ai", "music")
    assert rc == 0, out
    inst = next(c for c in calls if c.startswith("install.sh"))
    assert inst.startswith("install.sh --music")
    assert f"ACESTEP_DIR={box.app}/ACE-Step-1.5" in inst, "a clone under /root is unreadable by the service"
    assert box.status()["features"]["music"] is True


def test_installing_a_feature_never_switches_a_stopped_server_on(box):
    box.run("enable")
    box.run("disable")
    rc, out, calls = box.run("install-ai", "searxng")
    assert rc == 0, out
    assert not [c for c in calls if c.startswith(("systemctl restart", "systemctl enable", "systemctl start"))]


# ---------------------------------------------------------------------------------------- off
def test_disable_stops_the_server_and_nothing_else(box):
    box.run("enable")
    rc, out, calls = box.run("disable")
    assert rc == 0, out
    assert "systemctl disable --now posterchanai.service" in calls
    assert not [c for c in calls if "postgresql" in c], calls
    s = box.status()
    assert s["enabled"] == "disabled" and s["active"] == "inactive"


def test_logs_are_a_bounded_journal_read(box):
    rc, out, calls = box.run("logs")
    assert rc == 0
    assert calls == ["journalctl -u posterchanai.service -n 300 --no-pager -o short-iso"]


# ---------------------------------------------------------------------------------------- the grant
SUDOERS = ROOT / "os" / "overlay" / "app-misc" / "posterchan-server" / "files" / "posterchan-server.sudoers"


def test_the_sudo_rule_names_every_verb_and_nothing_else():
    rules = [l for l in SUDOERS.read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert len(rules) == 1, rules
    rule = rules[0].replace("\\", "")
    assert rule.startswith("%wheel ALL=(root) NOPASSWD:"), "administrators only — not every signed-in identity"
    cmds = [c.strip() for c in rule.split("NOPASSWD:", 1)[1].split(",")]
    expected = {f"/usr/local/bin/pc-server {v}" for v in
                ("status", "enable", "disable", "restart", "logs", "job", "job-log")}
    expected |= {f"/usr/local/bin/pc-server install-ai {f}" for f in ("ai", "music", "video", "voice", "searxng")}
    assert set(cmds) == expected
    assert not [c for c in cmds if "*" in c or c.endswith("pc-server")], "a wildcard or bare path = any arguments"
    visudo = shutil.which("visudo")
    if visudo:
        r = subprocess.run([visudo, "-c", "-f", str(SUDOERS)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_the_packaged_helper_is_the_one_in_the_repo():
    """The ebuild installs FILESDIR/pc-server (publish_overlay.sh re-injects it from os/bin on every publish);
    the committed copy must not drift in between, or a hand-built overlay ships an older helper."""
    packaged = ROOT / "os/overlay/app-misc/posterchan-server/files/pc-server"
    assert packaged.read_bytes() == HELPER.read_bytes()
    assert os.access(packaged, os.X_OK) and os.access(HELPER, os.X_OK)


def test_every_program_the_helper_runs_is_provided():
    """The helper's commands, read out of it, each with the package that provides it on PosterChanOS."""
    import re
    src = HELPER.read_text()
    provided = {"systemctl": "sys-apps/systemd", "systemd-run": "sys-apps/systemd",
                "journalctl": "sys-apps/systemd", "emerge": "sys-apps/portage",
                "useradd": "sys-apps/shadow", "chown": "sys-apps/coreutils"}
    for cmd in provided:
        assert re.search(r"(^|[\s(|;])" + re.escape(cmd) + r"\s", src), f"{cmd} no longer used — drop it here"
    ebuild = next((ROOT / "os/overlay/app-misc/posterchan-server").glob("posterchan-server-*.ebuild")).read_text()
    assert "dev-db/postgresql" not in ebuild, "the server needs no database server (#161)"


def test_enable_brings_searxng_with_the_server_and_starts_it(box):
    """ "I thought SearXNG was part of the normal install … but it has a separate button." It is part
    of the normal install now (install.sh --nostr-only runs setup_searxng, as the Full path always
    did), and enable starts its unit — no second install step, no separate button."""
    rc, out, calls = box.run("enable")
    assert rc == 0, box.job_log()
    assert not [c for c in calls if c.startswith("install.sh --searxng")], "already installed by --nostr-only"
    assert "systemctl enable --now posterchanai-searxng.service" in "\n".join(calls)
    assert box.status()["features"]["searxng"] is True


def test_a_server_set_up_before_searxng_came_with_it_gets_it_on_enable(box):
    rc, out, calls = box.run("enable", STUB_OLD_INSTALLER="1")
    assert rc == 0, box.job_log()
    assert [c for c in calls if c.startswith("install.sh --searxng")], "the backfill must install it once"
    assert "systemctl enable --now posterchanai-searxng.service" in "\n".join(calls)


def test_enable_installs_tor_when_the_machine_has_none(box):
    """Measured on the build VM: with no `tor` binary the app silently reached every upstream relay
    DIRECT from the machine's own IP. enable must install it."""
    rc, out, calls = box.run("enable")
    assert rc == 0, box.job_log()
    assert any(c.startswith("emerge -1 --usepkg --getbinpkg net-vpn/tor") for c in calls), calls


def test_enable_leaves_an_existing_tor_alone(box):
    tor = box.bin / "tor"
    tor.write_text("#!/bin/sh\nexit 0\n"); tor.chmod(0o755)
    rc, out, calls = box.run("enable")
    assert rc == 0, box.job_log()
    assert not any("net-vpn/tor" in c for c in calls), calls


def test_searxng_gets_setuptools_before_its_no_isolation_build():
    src = (ROOT / "scripts/install/searxng.sh").read_text()
    a = src.index('"$venv/bin/pip" install -q setuptools wheel')
    b = src.index('--no-build-isolation -e "$src_dir"')
    assert a < b, "setuptools must be in the venv before the --no-build-isolation install"


def test_searxng_runs_as_the_service_account_and_needs_no_database(box):
    """Measured on the build VM: the SearXNG unit ran as root. The installer is told the service user. There used
    to be a drop-in carrying the peer-socket DATABASE_URL too; with no database (#161) there is nothing to carry."""
    rc, out, calls = box.run("enable")
    assert rc == 0, box.job_log()
    inst = [c for c in calls if c.startswith("install.sh")]
    assert inst, calls
    assert not (box.units / "posterchanai-searxng.service.d").exists(), "no drop-in, no database URL"
    src = (ROOT / "os/bin/pc-server").read_text()
    assert 'PC_SERVICE_USER="$SVC_USER"' in src


def test_searxng_deps_include_curl_cffi():
    req = (ROOT / "requirements.txt").read_text()
    block = req[req.index("# >>> searxng-deps"):req.index("# <<< searxng-deps")]
    assert "curl_cffi" in block, "upstream SearXNG imports curl_cffi; without it the instance never starts"


# ---- PosterChanDB's directory is NOCOW on btrfs, on EVERY enable ----------------------------------------
# "make sure to do chattr -R +C on the posterchandb dir on the machines and default install": install.sh and
# gentoo.sh set it, but an enable on a box that is already set up never runs the installer, so the relay would
# create /var/lib/posterchandb itself with copy-on-write on.
NOCOW_STUBS = {
    "stat": '''if [ "$1" = "-f" ]; then echo "${STUB_FS:-btrfs}"; exit 0; fi; exec /usr/bin/stat "$@"''',
    "chattr": 'echo "chattr $*" >> "$STUB_LOG"',
}


@pytest.mark.parametrize("fs,expect", [("btrfs", True), ("ext2/ext3", False)])
def test_every_enable_makes_the_posterchandb_dir_nocow_on_btrfs(box, fs, expect):
    for name, body in NOCOW_STUBS.items():
        (box.bin / name).write_text("#!/bin/bash\n" + body + "\n")
        (box.bin / name).chmod(0o755)
    pcdb = box.tmp / "var" / "lib" / "posterchandb"
    env = {"PC_PCDB_DIR": str(pcdb), "STUB_FS": fs}
    assert box.run("enable", **env)[0] == 0
    box.run("disable", **env)
    (pcdb / "segment-0001.log").write_text("x")             # the relay has written since
    rc, out, calls = box.run("enable", **env)                 # already set up: the installer is NOT run
    assert rc == 0, out
    assert not any(c.startswith("install.sh") for c in calls)
    assert pcdb.is_dir()
    chattrs = [c for c in calls if c.startswith("chattr")]
    if expect:
        assert chattrs == [f"chattr +C {pcdb}"], calls       # holds data: new files inherit it
    else:
        assert not chattrs, calls


def test_an_empty_posterchandb_dir_gets_nocow_recursively(box):
    for name, body in NOCOW_STUBS.items():
        (box.bin / name).write_text("#!/bin/bash\n" + body + "\n")
        (box.bin / name).chmod(0o755)
    pcdb = box.tmp / "var" / "lib" / "posterchandb"
    rc, out, calls = box.run("enable", PC_PCDB_DIR=str(pcdb))
    assert rc == 0, out
    assert f"chattr -R +C {pcdb}" in calls, calls
