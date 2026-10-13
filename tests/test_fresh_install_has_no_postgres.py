"""A FRESH INSTALL HAS NO POSTGRESQL, IN ANY PATH (#161, owner: "the entire point of this was to avoid postgres").

The app's tables are relay documents and the relay stores into PosterChanDB, so nothing may install, initialise,
start, configure or depend on a PostgreSQL server. Every install path is checked by RUNNING it where it can be run:

  * install.sh -- `--nostr-only`, `--ai` (the Full flow with every question defaulted) and the interactive
    option-6 UPDATE, under bash with every system command stubbed (the harness of test_install_noninteractive.py).
    No call may reach a Postgres tool, nothing may write a DSN, and an update must leave a Postgres that is already
    there -- its data directory and the DSN another install wrote -- exactly as it was;
  * docker-compose.yml -- parsed (anchors and `<<` merges resolved): no postgres service, no depends_on on one, no
    DATABASE_URL/NOSTR_RELAY_PG_DSN in any service, no Postgres data volume;
  * docker-entrypoint.sh / Dockerfile -- no command (comments aside) that waits on or installs Postgres;
  * PosterChanOS -- `pc-server enable` RUN with its stubs on a machine that HAS PostgreSQL installed: it must not
    initialise, start or configure it; gentoo.sh's server package set and the server ebuild must not pull it in;
  * the relay -- a fresh process with no DSN and an empty data directory, after the app's own init_db() has applied
    every default, picks PRIMARY and opens a new PosterChanDB store, with psycopg2 UNIMPORTABLE;
  * the LiveCD server gate counts the posts it waits for by asking the relay, not a Postgres -- and that query is run
    here against the shipped relay on a real socket.
"""
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PG_TOOLS = ("psql", "createdb", "createuser", "initdb", "pg_ctl", "pg_ctlcluster", "pg_isready", "pg_dump",
            "dropdb", "postgres", "postgresql-setup")
_PG_WORD = re.compile(r"postgres|psql|initdb|pg_ctl|pg_isready|createdb|PG_DSN|DATABASE_URL", re.I)


def _stub_pg_tools(bindir: Path) -> None:
    """Every Postgres client/server tool, logging if it is ever run (STUB_LOG is set by both harnesses)."""
    for t in PG_TOOLS:
        (bindir / t).write_text('#!/bin/bash\necho "PGTOOL %s $*" >> "$STUB_LOG"\nexit 0\n' % t)
        (bindir / t).chmod(0o755)


# ---------------------------------------------------------------------------------------- install.sh
@pytest.fixture
def co(tmp_path):
    from tests.test_install_noninteractive import Checkout
    c = Checkout(tmp_path)
    _stub_pg_tools(c.bin)
    return c


def _no_postgres_touched(calls, where):
    bad = [c for c in calls if c.startswith("PGTOOL") or _PG_WORD.search(c)]
    assert not bad, f"{where} touched PostgreSQL: {bad}"


def _no_dsn_written(co):
    sec = co.src / "data" / "secrets.env"
    text = sec.read_text() if sec.exists() else ""
    assert "DATABASE_URL" not in text and "NOSTR_RELAY_PG_DSN" not in text, text
    for unit in (co.root / "etc/systemd/system").glob("*.service"):
        assert not _PG_WORD.search(unit.read_text()), f"{unit.name} names PostgreSQL:\n{unit.read_text()}"


def test_the_nostr_only_install_never_touches_postgres(co):
    rc, out, calls = co.run("--nostr-only", "--service-user", "nobody", "--no-start")
    assert rc == 0, out[-3000:]
    _no_postgres_touched(calls, "install.sh --nostr-only")
    _no_dsn_written(co)
    assert "PostgreSQL" not in out, "the installer still talks about setting up PostgreSQL"


def test_the_full_install_never_touches_postgres(co):
    rc, out, calls = co.run("--ai", "--backend", "cpu", "--service-user", "nobody", "--no-start")
    assert rc == 0, out[-4000:]
    assert "Installation Complete!" in out, "the run must reach the installer's own summary"
    _no_postgres_touched(calls, "install.sh --ai")
    _no_dsn_written(co)


def test_an_update_neither_needs_nor_creates_nor_removes_postgres(co):
    """Option 6 on a host that already runs a PostgreSQL (another program's, or the one a pre-#161 install used):
    the update must not call it, must not need it, and must leave its data and the DSN an older install wrote."""
    pgdata = co.root / "var/lib/postgresql/18/data"
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("18\n")
    (co.src / "data").mkdir(exist_ok=True)
    old = "export NOSTR_RELAY_PG_DSN='host=127.0.0.1 dbname=akkoma user=akkoma'\n"
    (co.src / "data" / "secrets.env").write_text(old)
    rc, out, calls = co.run(stdin="1\n6\n")
    assert rc == 0, out[-3000:]
    assert "Update complete." in out, out[-3000:]
    _no_postgres_touched(calls, "install.sh option 6 (update)")
    assert (pgdata / "PG_VERSION").read_text() == "18\n", "an update must never touch an existing cluster"
    assert (co.src / "data" / "secrets.env").read_text() == old, "an update must leave an existing DSN alone"


def test_no_install_module_provisions_postgres():
    """Every module install.sh sources: none may still define the provisioning step that used to run in all three
    paths. (Its file is gone; a re-added one would be sourced from install.sh, which is checked by running it.)"""
    assert not (ROOT / "scripts/install/postgres.sh").exists()
    inst = (ROOT / "install.sh").read_text()
    assert "setup_postgres" not in inst and "postgres.sh" not in inst
    for f in sorted((ROOT / "scripts/install").glob("*.sh")):
        code = "\n".join(l for l in f.read_text().splitlines() if not l.lstrip().startswith("#"))
        assert not re.search(r"\b(psql|initdb|pg_ctl|createdb)\b", code), f"{f.name} runs a Postgres tool"


# ---------------------------------------------------------------------------------------- docker
def _compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_compose_has_no_postgres_service_dependency_dsn_or_volume():
    d = _compose()
    for name, svc in d["services"].items():
        assert "postgres" not in name.lower(), f"service {name}"
        assert "postgres" not in str(svc.get("image", "")).lower(), f"{name} runs {svc.get('image')}"
        deps = svc.get("depends_on") or {}
        deps = list(deps) if isinstance(deps, (list, dict)) else [deps]
        assert not [x for x in deps if "postgres" in str(x).lower()], f"{name} depends on {deps}"
        env = svc.get("environment") or []
        keys = [e.split("=", 1)[0] for e in env] if isinstance(env, list) else list(env)
        assert not {"DATABASE_URL", "NOSTR_RELAY_PG_DSN", "POSTGRES_PASSWORD"} & set(keys), f"{name}: {keys}"
        for v in svc.get("volumes") or []:
            assert "/var/lib/postgresql" not in str(v), f"{name} mounts {v}"
    assert not [v for v in (d.get("volumes") or {}) if "pg" in v.split("-")], d.get("volumes")


def test_compose_services_still_carry_the_relay_store():
    """The other half: removing the database must not remove where the relay's store lives. The image sets
    POSTERCHANDB_DIR under the data volume, and every app service mounts that volume."""
    assert "POSTERCHANDB_DIR=/var/lib/posterchanai/posterchandb" in (ROOT / "Dockerfile").read_text()
    d = _compose()
    for name in ("cpu", "nostr", "cuda", "rocm", "intel", "relay", "worker", "media"):
        vols = [str(v) for v in d["services"][name].get("volumes") or []]
        assert any(v.startswith("pc-data:/var/lib/posterchanai") for v in vols), (name, vols)


def _code_lines(path: Path):
    for line in path.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            yield s


def test_the_entrypoint_and_image_neither_wait_on_nor_install_postgres():
    for f in ("docker-entrypoint.sh", "Dockerfile"):
        bad = [l for l in _code_lines(ROOT / f)
               if re.search(r"\b(psql|pg_isready|initdb|pg_ctl|createdb|postgresql(-\S+)?)\b|DATABASE_URL|PG_DSN", l)]
        assert not bad, f"{f}: {bad}"


def test_no_unit_orders_itself_after_postgresql():
    for f in [*ROOT.glob("posterchanai*.service"), ROOT / "scripts/install_services.sh",
              ROOT / "scripts/install/systemd.sh"]:
        assert "postgresql.service" not in f.read_text(), f.name


# ---------------------------------------------------------------------------------------- PosterChanOS
@pytest.fixture
def box(tmp_path):
    from tests.test_pc_server_helper import Box
    b = Box(tmp_path)       # a machine that HAS PostgreSQL 17 and 18 installed
    _stub_pg_tools(b.bin)
    return b


def test_pc_server_enable_never_touches_the_postgres_the_machine_has(box):
    rc, out, calls = box.run("enable")
    assert rc == 0, out
    log = box.job_log()
    assert "PosterChan server enabled" in log, log
    _no_postgres_touched(calls, "pc-server enable")
    assert not [c for c in calls if c.startswith("emerge --config")], calls
    assert not (box.pgdata / "18" / "data").exists(), "a cluster was initialised"
    secrets = (box.app / "data" / "secrets.env").read_text()
    assert "DATABASE_URL" not in secrets and "NOSTR_RELAY_PG_DSN" not in secrets, secrets
    assert 'POSTERCHANDB_DIR=/var/lib/posterchandb' in secrets
    assert not list(box.units.glob("*.d/*.conf")), "no drop-in carries a database URL any more"
    s = box.status()
    assert "postgres" not in s and s.get("database") == "posterchandb", s
    assert s["active"] == "active"


def test_pc_server_disable_leaves_a_postgres_an_older_version_set_up(box):
    """A machine enabled before #161 has the marker the old helper wrote. Its database may still be what that
    server's relay runs on, or another program's: disable must not stop it, and enable must not start it."""
    box.home.mkdir(parents=True, exist_ok=True)
    (box.home / "postgres-owned").write_text("18\n")
    assert box.run("enable")[0] == 0
    rc, out, calls = box.run("disable")
    assert rc == 0, out
    assert "systemctl disable --now posterchanai.service" in calls
    _no_postgres_touched(calls, "pc-server disable")


def test_posterchanos_does_not_ship_a_postgres_server():
    g = (ROOT / "os/gentoo.sh").read_text()
    m = re.search(r'^POSTERCHANOS_PACKAGES="(.*?)"', g, re.S | re.M)
    assert m, "POSTERCHANOS_PACKAGES not found"
    pkgs = m.group(1).replace("\\\n", " ").split()
    assert pkgs and "gui-wm/wayfire" in pkgs, pkgs
    assert not [p for p in pkgs if "postgresql" in p], pkgs
    ebuilds = sorted((ROOT / "os/overlay/app-misc/posterchan-server").glob("posterchan-server-*.ebuild"))
    assert len(ebuilds) == 1, ebuilds
    rdep = re.search(r'^RDEPEND="(.*?)"', ebuilds[0].read_text(), re.S | re.M).group(1)
    assert "postgresql" not in rdep, rdep
    assert "dev-lang/python:3.13" in rdep
    for f in (ROOT / "os/overlay").rglob("*.ebuild"):
        assert "dev-db/postgresql" not in re.sub(r"(?m)^\s*#.*$", "", f.read_text()), f


def test_the_settings_page_does_not_promise_a_database_setup():
    src = (ROOT / "static/js/client/os.js").read_text()
    seg = src[src.index("Not set up. Enabling"):src.index("btn.disable.onclick")]
    assert "PostgreSQL" not in seg and "s.postgres" not in seg, "the server page still describes a Postgres"


# ---------------------------------------------------------------------------------------- the relay
_FRESH_RELAY = textwrap.dedent('''
    import asyncio, sys
    sys.modules["psycopg2"] = None          # a fresh install has no reason to have a Postgres driver loaded
    from app.database import init_db
    init_db()                               # every default applied, as the app does at startup
    from app.services.nostr_relay import thread, pcdb_store
    loop = asyncio.new_event_loop()
    store = thread.open_store_for_settings(loop)
    kind = type(store).__name__
    store.close()
    print("RESULT", kind)
''')


def _fresh_env(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "NOSTR_RELAY_PG_DSN", "POSTERCHANDB_MODE", "PGHOST", "PGDATABASE")}
    env.update({"POSTERCHANAI_DATA": str(tmp_path / "data"),
                "POSTERCHANAI_LOCAL_SETTINGS": str(tmp_path / "data" / "local_settings.json"),
                "POSTERCHANDB_DIR": str(tmp_path / "pcdb"), "PYTHONPATH": str(ROOT)})
    (tmp_path / "data").mkdir()
    return env


def test_a_fresh_relay_with_no_dsn_and_an_empty_directory_is_primary_and_opens(tmp_path):
    p = subprocess.run([sys.executable, "-c", _FRESH_RELAY], cwd=ROOT, env=_fresh_env(tmp_path),
                       capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, (p.stdout + p.stderr)[-4000:]
    line = [l for l in p.stdout.splitlines() if l.startswith("RESULT ")]
    assert line == ["RESULT PcdbRelayStore"], p.stdout[-2000:] + p.stderr[-2000:]
    assert (tmp_path / "pcdb" / "relay").is_dir() and os.listdir(tmp_path / "pcdb" / "relay"), \
        "the primary store was not created"


def test_the_default_settings_name_no_postgres(monkeypatch):
    monkeypatch.delenv("NOSTR_RELAY_PG_DSN", raising=False)
    from app import database
    from app.services import settings_store
    monkeypatch.setattr(settings_store, "apply_defaults", lambda d: None)
    monkeypatch.setattr(settings_store, "load_local", lambda: None)
    database.init_db()
    assert database.DEFAULT_SETTINGS.get("nostr_relay_pg_dsn", "") == ""


# ---------------------------------------------------------------------------------------- the LiveCD gate
def test_the_livecd_server_gate_counts_posts_on_the_relay_not_in_postgres(tmp_path):
    """The gate's own query, run here against the shipped relay on PosterChanDB with two kind-1 posts in it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("livecd_gate", ROOT / "scripts/check_livecd_install_vm.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent = []

    def ask(cmd, pattern, _timeout):
        sent.append(cmd)
        return "9"
    assert mod._posts_flow(ask, tmp_path, clock=lambda: 0, sleep=lambda _s: None, poll=1, timeout=1) == 0
    q = sent[0]
    assert not _PG_WORD.search(q), q
    code = re.search(r'-c "(.*)" 2>&1', q).group(1).replace('\\"', '"')

    from tests.test_doc_table import SK, _Relay
    from app.services.nostr.event import build_event
    from websockets.sync.client import connect
    relay = _Relay(str(tmp_path / "relay"))
    try:
        with connect("ws://127.0.0.1:%d/relay" % relay.port) as ws:
            for i in range(2):
                ev = build_event(SK, 1, "post %d" % i)
                ws.send(json.dumps(["EVENT", ev]))
                while True:
                    m = json.loads(ws.recv(timeout=10))
                    if m[0] == "OK" and m[1] == ev["id"]:
                        assert m[2], m
                        break
        code = code.replace("port=3052", "port=%d" % relay.port)
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PYTHONPATH": str(ROOT)})
    finally:
        relay.close()
    assert p.stdout.strip().splitlines()[-1:] == ["POSTS=2"], p.stdout + p.stderr


def test_the_admin_offers_no_store_mode_at_all():
    """It used to default to "Off — PostgreSQL only" (the page told every fresh install its relay ran on a database it
    does not have); then to Automatic. Now there is nothing to choose: no install has a Postgres to choose
    (tests/test_admin_offers_no_postgres.py)."""
    html = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text()
    assert 'id="posterchandb_mode"' not in html and "PostgreSQL" not in html
