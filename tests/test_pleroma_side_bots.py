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
from tests import scratch_postgres

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTS = os.path.join(ROOT, "botframework")
DSN = scratch_postgres.params()   # a test Postgres -- see tests/scratch_postgres.py


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
                  f"inserted_at timestamp without time zone, ap_id text)")
        c.execute(f"CREATE TABLE {schema}.following_relationships (id serial PRIMARY KEY, follower_id uuid, "
                  f"following_id uuid, state integer)")
        # Akkoma's real column order -- the old blockbot read row[3], which is updated_at
        c.execute(f"CREATE TABLE {schema}.activities (id uuid PRIMARY KEY, data jsonb, "
                  f"inserted_at timestamp without time zone, updated_at timestamp without time zone)")
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

    def add(nick, local=True, minutes_ago=1):
        with admin.cursor() as c:
            c.execute(f"INSERT INTO {schema}.users VALUES (%s, %s, %s, (now() at time zone 'utc') - make_interval(mins => %s))",
                      (str(uuid.uuid4()), nick, local, minutes_ago))
    def block(actor, target, minutes_ago=0.0):
        import json as _j
        with admin.cursor() as c:
            c.execute(f"INSERT INTO {schema}.activities VALUES (%s, %s, "
                      f"(now() at time zone 'utc') - make_interval(secs => %s), (now() at time zone 'utc') + interval '7 days')",
                      (str(uuid.uuid4()), _j.dumps({"type": "Block", "actor": actor, "object": target}), minutes_ago * 60))
    add.block = block

    def follow(follower_ap, followed_ap):
        ids = []
        with admin.cursor() as c:
            for ap in (follower_ap, followed_ap):
                c.execute(f"SELECT id FROM {schema}.users WHERE ap_id = %s", (ap,))
                row = c.fetchone()
                if not row:
                    uid = str(uuid.uuid4())
                    c.execute(f"INSERT INTO {schema}.users (id, nickname, local, inserted_at, ap_id) "
                              f"VALUES (%s, %s, %s, now(), %s)", (uid, ap.rstrip('/').split('/')[-1],
                                                                  'detroitriotcity.com' in ap, ap))
                    row = (uid,)
                ids.append(row[0])
            c.execute(f"INSERT INTO {schema}.following_relationships (follower_id, following_id, state) "
                      f"VALUES (%s, %s, 2) RETURNING id", tuple(ids))
            return c.fetchone()[0]

    def unfollow(rel_id):
        with admin.cursor() as c:
            c.execute(f"DELETE FROM {schema}.following_relationships WHERE id = %s", (rel_id,))
    add.follow, add.unfollow, add.schema = follow, unfollow, schema
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


def test_a_report_by_a_person_whose_name_contains_a_bot_name_is_announced(reports, monkeypatch):
    """`news` in BOT_BLACKLIST used to swallow every report filed by @newsom or @goodnews."""
    rb, queue, posted = reports
    monkeypatch.setattr(rb, "BOT_BLACKLIST", ["news"])
    queue["reports"] = [_report(1)]
    rb.report_pleroma()
    queue["reports"] = [_report(1), _report(2, reporter="goodnews", target="newsom@other.example")]
    rb.report_pleroma()
    assert len(posted) == 1, "a person's report was dropped as a bot's"


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
    stub = type(sys)("nostr")
    stub.post_image_to_fediverse = lambda m, *a, **k: sent.append(m)
    monkeypatch.setitem(sys.modules, "nostr", stub)        # restores a real `nostr` loaded earlier, too
    assert hb.post_to_nostr("this is forbidden") is False and sent == []
    assert hb.post_to_nostr("fine") is True and sent == ["fine"]


# ============================== Pleroma block bot (real Postgres) ==================================

@pytest.fixture
def blockbot(monkeypatch, tmp_path, akkoma):
    bb = _fresh(monkeypatch, "blockbot", SQL_DATABASE=DSN["dbname"], SQL_USER=DSN["user"], SQL_PASS="x",
                SQL_HOST=DSN["host"], PLEROMA_ENDPOINT="https://detroitriotcity.com", AUTO_NARRATE="false",
                OPENAI_ENDPOINT="")
    monkeypatch.setattr(bb, "LAST_BLOCK_ID_FILE", str(tmp_path / "last_block"))
    monkeypatch.setattr(bb, "BLOCK_IMAGE", "")
    monkeypatch.setattr(bb, "OPENAI_ENDPOINT", "")
    posted = []
    monkeypatch.setattr(bb, "pleroma_post_image_to_fediverse", lambda msg, *a, **k: posted.append(msg))
    bb.conn = None
    bb.init_db()
    return bb, posted, akkoma.block


A, B = "https://detroitriotcity.com/users/alice", "https://poa.st/users/bob"


def test_the_first_look_announces_no_history(blockbot):
    bb, posted, block = blockbot
    block(A, B, minutes_ago=30)
    bb.blocks()
    assert posted == [], "the first poll announced a block from before the bot started"


def test_a_new_block_is_announced_once_with_full_handles(blockbot):
    bb, posted, block = blockbot
    bb.blocks()
    block(A, B)
    bb.blocks()
    bb.blocks()
    assert len(posted) == 1, posted
    assert "@alice@detroitriotcity.com" in posted[0] and "@bob@poa.st" in posted[0], posted[0]


def test_a_late_poll_catches_every_block_since_the_last(blockbot, monkeypatch):
    """The old rule announced only the PREVIOUS CLOCK MINUTE; a late poll lost the rest for good."""
    bb, posted, block = blockbot
    import datetime as dt
    earlier = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=10)).isoformat()
    open(bb.LAST_BLOCK_ID_FILE, "w").write(earlier)       # the bot last looked 10 minutes ago
    block(A, B, minutes_ago=7)
    block("https://poa.st/users/carol", A, minutes_ago=2)
    bb.blocks()
    assert len(posted) == 1 and "@bob@poa.st" in posted[0] and "@carol@poa.st" in posted[0], posted


def test_a_failed_post_is_tried_again(blockbot, monkeypatch):
    bb, posted, block = blockbot
    bb.blocks()
    block(A, B)

    def boom(*a, **k):
        raise RuntimeError("pleroma down")
    monkeypatch.setattr(bb, "pleroma_post_image_to_fediverse", boom)
    bb.blocks()
    monkeypatch.setattr(bb, "pleroma_post_image_to_fediverse", lambda msg, *a, **k: posted.append(msg))
    bb.blocks()
    assert len(posted) == 1, "a block whose post failed was lost"


# ============================== unfollow bot (real Postgres) =======================================

@pytest.fixture
def unfollows(monkeypatch, tmp_path, akkoma):
    ub = _fresh(monkeypatch, "unfollowbot", SQL_DATABASE=DSN["dbname"], SQL_USER=DSN["user"], SQL_PASS="x",
                SQL_HOST=DSN["host"], PLEROMA_ENDPOINT="https://detroitriotcity.com", AUTO_NARRATE="false",
                OPENAI_ENDPOINT="")
    monkeypatch.setattr(ub, "PLEROMA_FOLLOWING_SNAPSHOT_FILE", str(tmp_path / "snapshot.json"))
    monkeypatch.setattr(ub, "UNFOLLOW_IMAGE", "")
    monkeypatch.setattr(ub, "OPENAI_ENDPOINT", "")
    posted = []
    monkeypatch.setattr(ub, "pleroma_post_image_to_fediverse", lambda msg, *a, **k: posted.append(msg))
    ub.conn = None
    ub.init_db()                                              # connect exactly as the bot does
    return ub, posted, akkoma


LOCAL, REMOTE = "https://detroitriotcity.com/users/alice", "https://poa.st/users/bob"


def test_a_remote_unfollow_of_a_member_is_announced_once(unfollows):
    ub, posted, db = unfollows
    rel = db.follow(REMOTE, LOCAL)
    db.follow(LOCAL, REMOTE)                                  # our member following out: not tracked
    db.follow("https://detroitriotcity.com/users/carol", LOCAL)   # local -> local: not tracked
    ub.pleroma_unfollows()
    assert posted == [], "the first look announced something"
    db.unfollow(rel)
    ub.pleroma_unfollows()
    ub.pleroma_unfollows()
    assert len(posted) == 1 and "@bob@poa.st unfollowed @alice@detroitriotcity.com" in posted[0], posted


def test_a_database_outage_announces_nothing_and_keeps_the_snapshot(unfollows, monkeypatch):
    """[] from a failed query used to read as "nobody follows anyone": every follow was announced
    as an unfollow and the empty list overwrote the snapshot."""
    ub, posted, db = unfollows
    db.follow(REMOTE, LOCAL)
    db.follow("https://poa.st/users/dave", LOCAL)
    ub.pleroma_unfollows()
    before = open(ub.PLEROMA_FOLLOWING_SNAPSHOT_FILE).read()
    monkeypatch.setenv("PGOPTIONS", "-c search_path=no_such_schema")   # the tables vanish from view
    ub.conn.close()                                           # and the live connection drops
    ub.pleroma_unfollows()
    assert posted == [], f"a database outage announced {len(posted)} fake unfollow(s)"
    assert open(ub.PLEROMA_FOLLOWING_SNAPSHOT_FILE).read() == before, "the snapshot was overwritten"
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={db.schema}")
    ub.conn = None
    ub.pleroma_unfollows()
    assert posted == [], "recovering from the outage announced unfollows that never happened"


# ---- what the post SAYS ("Blockbot is adding that BLOCKER: nonsense still") -----------------------

NONSENSE = ("@StarProphet@fsebugoutzone.org blocked @bob@poa.st\nBLOCKER: @Starprophet@annihilation.social blocked "
            "@bob@poa.st\nBLOCKER: @StarProphet@merovingian.club blocked @bob@poa.st\nThis is not a joke. These are "
            "coordinated blocks from multiple platforms. The vampire brigade has been systematically silenced across "
            "the dark web. Alice and Judge Dread were at the center of this operation.")


def _with_ai(bb, monkeypatch, reply):
    monkeypatch.setattr(bb, "OPENAI_ENDPOINT", "http://ai.invalid")
    monkeypatch.setattr(bb, "generate_reply", lambda prompt: reply)


def test_the_post_is_plain_headlines_with_no_scaffolding(blockbot):
    bb, posted, block = blockbot
    bb.blocks()
    block(A, B)
    bb.blocks()
    assert posted == ["@alice@detroitriotcity.com blocked @bob@poa.st"], posted


def test_the_model_cannot_rewrite_the_post_or_invent_a_story(blockbot, monkeypatch):
    """The exact shape posted on 2026-09-30: repeated BLOCKER lines and a made-up conspiracy. The
    headline stays the database's, and commentary that names anybody is dropped."""
    bb, posted, block = blockbot
    bb.blocks()
    block(A, B)
    _with_ai(bb, monkeypatch, NONSENSE)
    bb.blocks()
    assert posted == ["@alice@detroitriotcity.com blocked @bob@poa.st"], posted
    assert "BLOCKER" not in posted[0] and "vampire" not in posted[0].lower()


def test_clean_commentary_goes_under_the_headline(blockbot, monkeypatch):
    bb, posted, block = blockbot
    bb.blocks()
    block(A, B)
    _with_ai(bb, monkeypatch, "Another bridge burned. The timeline got a little quieter.")
    bb.blocks()
    assert posted == ["@alice@detroitriotcity.com blocked @bob@poa.st\n\n"
                      "Another bridge burned. The timeline got a little quieter."], posted


def test_commentary_naming_anyone_in_the_batch_is_dropped_even_without_an_at():
    sys.path.insert(0, BOTS)
    import block_wording as w
    assert w.commentary("Starprophet strikes again.", names=["StarProphet"]) == ""
    assert w.commentary("BLOCKER: someone blocked someone. BLOCKER: someone blocked someone.") == ""
    assert w.commentary("Justice, served cold.", names=["StarProphet"]) == "Justice, served cold."
