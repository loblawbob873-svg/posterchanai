"""The fediverse-side bots do their jobs: welcome new members once, announce new reports once, post
the trending hashtags.

Run: venv-unified/bin/python -m pytest tests/test_pleroma_side_bots.py

These run on nas.lan against detroitriotcity (Akkoma) and had no tests at all. The welcome bot's SQL
runs here against a REAL Postgres -- a throwaway schema on the local server shaped like the Akkoma
`users` columns it reads, reached through PGOPTIONS so the bot's own connection code is what connects.
The report bot's admin-API call and the hashtag bot's scrapes are stubbed at the HTTP layer; every
decision above that (what is new, what was already done, what to skip, what to post) is the shipped
code.
"""
import importlib
import os
import sys
import uuid

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTS = os.path.join(ROOT, "botframework")
DSN = dict(host="127.0.0.1", port=5432, dbname="posterchan_relay", user="posterchan")


def _fresh(monkeypatch, name, **env):
    monkeypatch.syspath_prepend(BOTS)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for m in ("config", name):
        sys.modules.pop(m, None)
    return importlib.import_module(name)


# ============================== welcome bot (real Postgres) ========================================

@pytest.fixture
def akkoma(monkeypatch):
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        admin = psycopg2.connect(connect_timeout=5, **DSN)
    except Exception as e:
        pytest.skip(f"Postgres not reachable: {e}")
    admin.autocommit = True
    schema = "akkoma_" + uuid.uuid4().hex[:10]
    with admin.cursor() as c:
        c.execute(f"CREATE SCHEMA {schema}")
        c.execute(f"CREATE TABLE {schema}.users (id uuid PRIMARY KEY, nickname text, local boolean, "
                  f"inserted_at timestamp without time zone)")
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

    def add(nick, local=True, minutes_ago=1):
        with admin.cursor() as c:
            c.execute(f"INSERT INTO {schema}.users VALUES (%s, %s, %s, (now() at time zone 'utc') - make_interval(mins => %s))",
                      (str(uuid.uuid4()), nick, local, minutes_ago))
    yield add
    with admin.cursor() as c:
        c.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


@pytest.fixture
def welcome(monkeypatch, tmp_path, akkoma):
    wb = _fresh(monkeypatch, "welcomebot", SQL_DATABASE=DSN["dbname"], SQL_USER=DSN["user"], SQL_PASS="x",
                SQL_HOST=DSN["host"], WELCOME_LOOKBACK_MINUTES="60", AUTO_NARRATE="false", OPENAI_ENDPOINT="")
    monkeypatch.setattr(wb, "WELCOMED_PLEROMA_FILE", str(tmp_path / "welcomed"))
    monkeypatch.setattr(wb, "WELCOME_IMAGE", "")
    monkeypatch.setattr(wb, "get_instance_name", lambda: "Detroit Riot City")
    posted = []
    monkeypatch.setattr(wb, "pleroma_post_image", lambda msg, *a, **k: posted.append(msg))
    wb.conn = None
    wb.init_db()
    assert wb.conn is not None, "the bot could not connect to the test database"
    return wb, posted, akkoma


def test_a_new_local_member_is_welcomed_once(welcome):
    wb, posted, add = welcome
    add("newbie")
    wb.welcome_pleroma()
    assert len(posted) == 1 and "newbie" in posted[0], posted
    wb.welcome_pleroma()
    assert len(posted) == 1, "the same member was welcomed twice"


def test_only_real_new_local_accounts_are_welcomed(welcome):
    wb, posted, add = welcome
    add("remote_person", local=False)
    add("internal.fetch")
    add("relay")
    add("oldtimer", minutes_ago=600)                        # joined before the lookback window
    wb.welcome_pleroma()
    assert posted == [], posted


def test_a_failed_post_is_retried_next_time(welcome, monkeypatch):
    wb, posted, add = welcome
    add("unlucky")

    def boom(*a, **k):
        raise RuntimeError("pleroma down")
    monkeypatch.setattr(wb, "pleroma_post_image", boom)
    wb.welcome_pleroma()
    monkeypatch.setattr(wb, "pleroma_post_image", lambda msg, *a, **k: posted.append(msg))
    wb.welcome_pleroma()
    assert len(posted) == 1 and "unlucky" in posted[0], "a welcome that failed to post was never retried"


def test_the_welcome_bot_leaves_no_transaction_open(welcome):
    """The 2026-09-26 finding (see akkoma_db.py): a read must not hold the database open."""
    wb, posted, add = welcome
    add("someone")
    wb.welcome_pleroma()
    with wb.conn.cursor() as c:
        c.execute("SELECT state FROM pg_stat_activity WHERE pid = pg_backend_pid()")
    assert wb.conn.get_transaction_status() == 0, "idle in transaction after a poll"   # TRANSACTION_STATUS_IDLE


# ============================== report bot (admin API stubbed) =====================================

@pytest.fixture
def reports(monkeypatch, tmp_path):
    rb = _fresh(monkeypatch, "reportbot", PLEROMA_ENDPOINT="https://detroitriotcity.com",
                PLEROMA_ADMIN_TOKEN="tok", AUTO_NARRATE="false", OPENAI_ENDPOINT="")
    monkeypatch.setattr(rb, "LAST_PLEROMA_REPORT_ID_FILE", str(tmp_path / "last_report"))
    monkeypatch.setattr(rb, "REPORT_IMAGE", "", raising=False)
    queue, posted = {"reports": [], "fail": False}, []

    class _R:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    def get(url, headers=None, timeout=0):
        assert "/api/v1/pleroma/admin/reports" in url and headers["Authorization"] == "Bearer tok"
        if queue["fail"]:
            raise requests.ConnectionError("down")
        return _R({"reports": list(queue["reports"])})
    monkeypatch.setattr(rb.requests, "get", get)
    for name in ("pleroma_post_image", "post_image_to_fediverse", "pleroma_post"):
        if hasattr(rb, name):
            monkeypatch.setattr(rb, name, lambda msg, *a, **k: posted.append(msg))
    return rb, queue, posted


def _report(i, reporter="alice", target="bob", content="spam", created=None):
    return {"id": f"r{i}", "created_at": created or f"2026-09-26T10:00:{i:02d}Z", "content": content,
            "actor": {"acct": reporter}, "account": {"acct": target},
            "statuses": [{"url": f"https://detroitriotcity.com/notice/{i}"}]}


def test_the_first_look_only_records_what_exists(reports):
    rb, queue, posted = reports
    queue["reports"] = [_report(1), _report(2)]
    rb.report_pleroma()
    assert posted == [], "the first run announced every existing report"


def test_a_new_report_is_announced_once_with_full_addresses(reports):
    rb, queue, posted = reports
    queue["reports"] = [_report(1)]
    rb.report_pleroma()
    queue["reports"] = [_report(1), _report(2, reporter="carol", target="dave@other.example")]
    rb.report_pleroma()
    rb.report_pleroma()
    assert len(posted) == 1, posted
    assert "carol@detroitriotcity.com" in posted[0] and "dave@other.example" in posted[0], posted[0]


def test_a_report_involving_a_bot_is_not_announced(reports, monkeypatch):
    rb, queue, posted = reports
    monkeypatch.setattr(rb, "BOT_BLACKLIST", ["posterchan"])
    queue["reports"] = [_report(1)]
    rb.report_pleroma()
    queue["reports"] = [_report(1), _report(2, reporter="posterchan")]
    rb.report_pleroma()
    assert posted == [], "a bot-involved report was announced (bot-to-bot loop risk)"


def test_an_unreachable_admin_api_changes_nothing(reports):
    rb, queue, posted = reports
    queue["reports"] = [_report(1)]
    rb.report_pleroma()
    queue["fail"] = True
    rb.report_pleroma()
    queue["fail"] = False
    queue["reports"] = [_report(1), _report(2)]
    rb.report_pleroma()
    assert len(posted) == 1, "an outage lost or duplicated a report"


# ============================== hashtag bot (scrapes stubbed) ======================================

@pytest.fixture
def tags(monkeypatch):
    hb = _fresh(monkeypatch, "hashtagbot", PLATFORM_TYPE="nostr", NOSTR_NSEC="11" * 32)
    pages = {}

    class _R:
        def __init__(self, text=None, js=None, status=200):
            self.text, self._js, self.status_code = text or "", js, status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

        def json(self):
            return self._js

    def get(url, timeout=0, headers=None, **kw):
        if url in pages:                                    # exact first: fedi.buzz/ prefixes fedi.buzz/in/en
            return pages[url]
        for key, resp in pages.items():
            if key.startswith("/") and key in url:
                return resp
        raise requests.ConnectionError(url)
    monkeypatch.setattr(hb.requests, "get", get)
    return hb, pages, _R


def _html(*tags_):
    return "".join(f'<a href="https://fedi.buzz/tags/{t}">#{t}</a>' for t in tags_)


def test_trending_tags_are_english_ranked_and_cleaned(tags):
    hb, pages, R = tags
    pages[hb.FEDIBUZZ_ENGLISH_URL] = R(_html("nostr", "nostr", "nostr", "Art", "art", "猫", "12345", "ab",
                                             "x" * 40, "caf%C3%A9", "photography"))
    got = hb.fetch_fedibuzz_hashtags(limit=10, english_only=True)
    assert got[0] == "nostr" and got[1] == "art", got
    assert "photography" in got
    for bad in ("猫", "12345", "ab", "x" * 40, "café"):
        assert bad not in got, f"{bad!r} should have been filtered"


def test_a_short_list_falls_back_to_the_other_sources(tags):
    hb, pages, R = tags
    pages[hb.FEDIBUZZ_ENGLISH_URL] = R(_html("nostr"))
    pages[hb.FEDIBUZZ_URL] = R(_html("nostr", "bitcoin"))
    pages["/api/v1/trends/tags"] = R(js=[{"name": "linux"}, {"name": "bitcoin"}])
    got = hb.get_trending_hashtags(limit=3)
    assert got == ["nostr", "bitcoin", "linux"], got


def test_the_post_lists_every_tag_as_a_link(tags):
    hb, _, _ = tags
    msg = hb.format_hashtag_post(["nostr", "#art"])
    assert "#nostr" in msg and "#art" in msg and "##art" not in msg
    assert hb.format_hashtag_post([]) is None


def test_a_blocked_phrase_is_never_posted(tags, monkeypatch):
    hb, _, _ = tags
    monkeypatch.setattr(hb, "BLOCK_PHRASE", "forbidden")
    sent = []
    sys.modules["nostr"] = type(sys)("nostr")
    sys.modules["nostr"].post_image_to_fediverse = lambda m, *a, **k: sent.append(m)
    try:
        assert hb.post_to_nostr("this is forbidden") is False and sent == []
        assert hb.post_to_nostr("fine") is True and sent == ["fine"]
    finally:
        sys.modules.pop("nostr", None)
