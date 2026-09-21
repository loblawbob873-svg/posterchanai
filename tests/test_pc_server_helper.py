"""os/bin/pc-server — PosterChanOS's privileged door to the bundled server, RUN under bash with every system
command stubbed.

The stubs keep state (enabled/active units, the Postgres cluster, which users exist) in files, so a second
`enable` sees what the first one did. `systemd-run` runs the job SYNCHRONOUSLY with the environment the real
one would give it, so the whole enable → Postgres → installer → unit chain is observable from one call.

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
echo "install.sh $* | DATABASE_URL=${DATABASE_URL:-} HOME=$HOME ACESTEP_DIR=$ACESTEP_DIR" >> "$STUB_LOG"
[ -n "${STUB_INSTALL_FAIL:-}" ] && exit 1
case "$1" in
  --nostr-only) mkdir -p venv/bin venv/lib/python3.13/site-packages; touch venv/bin/python; chmod +x venv/bin/python
                echo "[Unit]" > "$PC_SERVER_UNIT_DIR/posterchanai.service"
                mkdir -p data; echo "export POSTERCHANAI_NOSTR_ONLY=1" >> data/secrets.env ;;
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
    assert s["postgres"]["slot"] == "18", "must pick the NEWEST installed server"
    assert s["features"] == {f: False for f in ("ai", "music", "video", "voice", "searxng")}
    assert s["job"]["running"] is False


# ---------------------------------------------------------------------------------------- enable
def test_enable_initialises_postgres_runs_the_installer_and_starts_the_unit(box):
    rc, out, calls = box.run("enable")
    assert rc == 0, out
    log = box.job_log()
    assert "PosterChan server enabled" in log, log
    text = "\n".join(calls)
    assert "useradd --system" in text, "the service account must exist before anything is chowned to it"
    assert "emerge --config dev-db/postgresql:18" in text
    # The cluster it made never runs on initdb's trust default, and the only door in is peer.
    hba = (box.pgetc / "postgresql-18" / "pg_hba.conf").read_text()
    assert "trust" not in hba.replace("# Written", "")
    assert "peer map=posterchan" in hba and "scram-sha-256" in hba
    ident = (box.pgetc / "postgresql-18" / "pg_ident.conf").read_text()
    assert "posterchan-server  posterchan" in ident
    assert calls.index(next(c for c in calls if c.startswith("systemctl enable --now postgresql-18"))) < \
        calls.index(next(c for c in calls if c.startswith("install.sh"))), "Postgres must be up before the installer"
    inst = next(c for c in calls if c.startswith("install.sh"))
    assert "install.sh --nostr-only --service-user posterchan-server --no-start" in inst
    assert "host=/run/postgresql" in inst, "the installer's DB steps must use the peer-mapped socket"
    assert "systemctl enable --now posterchanai.service" in text
    secrets = (box.app / "data" / "secrets.env").read_text()
    assert "DATABASE_URL='postgresql+psycopg2://posterchan@/posterchan_relay?host=/run/postgresql'" in secrets
    assert "NOSTR_RELAY_PG_DSN='host=/run/postgresql dbname=posterchan_relay user=posterchan'" in secrets
    assert f"chown -R posterchan-server:posterchan-server {box.app}" in text
    s = box.status()
    assert s["configured"] and s["venv"] and s["enabled"] == "enabled" and s["active"] == "active"
    assert s["nostrOnly"] is True and s["job"]["rc"] == "0"


def test_a_second_enable_neither_reinitialises_postgres_nor_reinstalls(box):
    assert box.run("enable")[0] == 0
    box.run("disable")
    rc, out, calls = box.run("enable")
    assert rc == 0, out
    text = "\n".join(calls)
    assert "emerge --config" not in text, "re-initialising would destroy the database"
    assert not any(c.startswith("install.sh") for c in calls), "an installed server is only started"
    assert "systemctl enable --now posterchanai.service" in text
    assert (box.app / "data" / "secrets.env").read_text().count("DATABASE_URL") == 1


def test_a_cluster_that_was_already_there_is_used_as_it_is(box):
    data = box.pgdata / "18" / "data"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("18")
    etc = box.pgetc / "postgresql-18"
    etc.mkdir(parents=True)
    (etc / "pg_hba.conf").write_text("somebody's own rules\n")
    rc, out, calls = box.run("enable")
    assert rc == 0, out
    assert "emerge --config" not in "\n".join(calls)
    assert (etc / "pg_hba.conf").read_text() == "somebody's own rules\n"
    assert "DATABASE_URL" not in (box.app / "data" / "secrets.env").read_text()
    # …and turning the server off must not take somebody else's database down with it.
    _, _, calls = box.run("disable")
    assert not [c for c in calls if "postgresql-18" in c]


def test_enable_without_postgres_fails_in_the_log_not_silently(tmp_path):
    b = Box(tmp_path, pg=False)
    rc, out, calls = b.run("enable")
    assert "PostgreSQL is not installed" in b.job_log()
    assert "rc=1" in (b.home / "job.state").read_text()
    assert not any(c.startswith("install.sh") for c in calls)


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
def test_disable_stops_the_server_and_the_database_it_brought_up(box):
    box.run("enable")
    rc, out, calls = box.run("disable")
    assert rc == 0, out
    assert "systemctl disable --now posterchanai.service" in calls
    assert "systemctl disable --now postgresql-18.service" in calls
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
                "journalctl": "sys-apps/systemd", "emerge": "sys-apps/portage", "runuser": "sys-apps/util-linux",
                "useradd": "sys-apps/shadow", "chown": "sys-apps/coreutils"}
    for cmd in provided:
        assert re.search(r"(^|[\s(|;])" + re.escape(cmd) + r"\s", src), f"{cmd} no longer used — drop it here"
    ebuild = next((ROOT / "os/overlay/app-misc/posterchan-server").glob("posterchan-server-*.ebuild")).read_text()
    assert "dev-db/postgresql[server]" in ebuild, "psql/initdb come from the server package"
