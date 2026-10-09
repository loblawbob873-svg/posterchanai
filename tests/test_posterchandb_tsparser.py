"""PosterChanDB search tokens are Postgres's search tokens — checked against a REAL Postgres, not a reading of its docs.

The relay searches with `to_tsvector('simple', content) @@ plainto_tsquery('simple', search)`. A word-splitter
disagreed with that on ~2/3 of real searches (URLs, hosts, hyphens, HTML…), so tsparser.py ports Postgres's own
parser. These tests hold it to the original, three ways:

  1. fixed cases whose expected tokens were taken from Postgres 18 (run with or without a Postgres);
  2. a seeded FUZZER — thousands of strings built from URLs, emails, hosts, paths, numbers, versions, HTML, entities,
     many scripts, combining marks, zero-width characters, emoji and odd whitespace — compared token by token
     (ts_debug) and lexeme by lexeme (to_tsvector) against the scratch Postgres, in the production locale (C.UTF-8);
  3. the search VERDICT: for random (content, query) pairs, whether we match == whether Postgres's `@@` matches.

Plus the fast path (per-whitespace-chunk, memoised) must equal the full state machine on everything fuzzed.
"""
import random

import pytest

from app.services.posterchandb import tsparser as T
from tests import scratch_postgres

psycopg2 = pytest.importorskip("psycopg2")

# ---------------------------------------------------------------- 1. fixed cases (expected = Postgres 18.4)
FIXED = {
    "https://x.com/image.jpg check this": [("protocol", "https://"), ("url", "x.com/image.jpg"), ("host", "x.com"),
                                           ("url_path", "/image.jpg"), ("blank", " "), ("asciiword", "check"),
                                           ("blank", " "), ("asciiword", "this")],
    "visit poster.place now": [("asciiword", "visit"), ("blank", " "), ("host", "poster.place"), ("blank", " "),
                               ("asciiword", "now")],
    "self-hosted": [("asciihword", "self-hosted"), ("hword_asciipart", "self"), ("blank", "-"),
                    ("hword_asciipart", "hosted")],
    "don't": [("asciiword", "don"), ("blank", "'"), ("asciiword", "t")],
    "v1.2.3": [("file", "v1.2.3")],
    "<b>bold</b> &amp;": [("tag", "<b>"), ("asciiword", "bold"), ("tag", "</b>"), ("blank", " "), ("entity", "&amp;")],
}
LEXEMES = {
    "Привет мир": {"привет", "мир"},
    "ÉCOLE Straße": {"école", "straße"},
    "İstanbul": {"istanbul"},                       # towlower, not Python's two-character 'i̇'
    "émoji 🎉 party": {"émoji", "party"},            # under C.UTF-8 an emoji is not a word
    "https://x.com/image.jpg": {"x.com/image.jpg", "x.com", "/image.jpg"},   # 'image' is NOT a search word
    "<script>var x=1</script> after": {"after"},     # script bodies are ignored
}


@pytest.mark.parametrize("text", sorted(FIXED))
def test_fixed_tokens(text):
    assert list(T.tokens(text)) == FIXED[text]


@pytest.mark.parametrize("text", sorted(LEXEMES))
def test_fixed_lexemes(text):
    assert T.search_set(text) == LEXEMES[text]


def test_an_empty_or_wordless_query_matches_nothing():
    assert T.query_lexemes("") == set() and T.query_lexemes(" ... <b> ") == set()


def test_overlong_words_are_skipped_like_postgres():
    w = "a" * 2047
    assert T.search_set(w + " ok") == {"ok"} and T.search_set("a" * 2046) == {"a" * 2046}


# ---------------------------------------------------------------- 2/3. against a real Postgres
FRAG = ["hello", "World", "nostr", "relay", "self-hosted", "x-ray-vision", "don't", "foo_bar", "a", "I",
        "https://example.com/a/b.jpg?x=1&y=2#f", "http://192.168.0.1:8080/p", "www.poster.place/path", "poster.place",
        "bob@example.com", "a.b.c", "v1.2.3", "1.2.3.4", "3.14", "-5", "+7", "1e10", "-1.5e-3", "0x1F", "abc123", "123abc",
        "/usr/local/bin", "~/file.txt", "./run.sh", "../up", "<b>", "</b>", "<a href=\"x y\">", "<!-- c -->", "&amp;",
        "&#169;", "&#x1F600;", "&bogus", "<script>alert(1)</script>", "<style>.a{}</style>", "Привет", "мир", "日本語",
        "のテキスト", "Straße", "İstanbul", "ÉCOLE", "naïve", "é", "हिन्दी", "कि", "١٢٣", "مرحبا", "שלום",
        "🎉", "👍🏽", "​", " ", " ", "\t", "\n", "#tag", "@user", "nostr:npub1qqq", "C++", "C#",
        "...", "--", "—", "'", '"', "(", ")", "[x]", "{y}", "|", "^", "`", "\\", "=", ";", ":", ",", "!", "?", "%",
        "AbCdEf+gh/ij==", "lnbc10u1p3xyz", "1,000", "12:30", "2026-10-09", "e.g.", "U.S.A.", "a-1", "1-a", "x-",
        "-x", "a@b", "a@b.c", "a@b.c.d/e", "foo.bar/", "x.com:", "x.com:80", "x.com:80/p", "ftp://h.io/f"]
SEPS = ["", "", " ", " ", "  ", "\n", ".", "-", "/", "@", ":", ",", "&", "<", ">", "_", "+"]


def _fuzz(n, seed):
    r = random.Random(seed)
    out = []
    for _ in range(n):
        k = r.randint(1, 10)
        out.append("".join(r.choice(FRAG) + r.choice(SEPS) for _ in range(k)))
    return out


@pytest.fixture(scope="module")
def pg():
    try:
        conn = psycopg2.connect(scratch_postgres.dsn(), connect_timeout=5)
    except Exception as e:      # noqa: BLE001
        pytest.skip("no Postgres to compare against: %s" % e)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT datctype FROM pg_database WHERE datname = current_database()")
    assert cur.fetchone()[0].lower().replace("-", "") in ("c.utf8",), \
        "the scratch Postgres must use production's locale (C.UTF-8), or lowercasing differs"
    yield cur
    conn.close()


def test_every_token_matches_postgres_ts_debug(pg):
    corpus = _fuzz(3000, 1009) + list(FIXED) + list(LEXEMES)
    pg.execute("""SELECT u.i, d.alias, d.token FROM unnest(%s::text[]) WITH ORDINALITY AS u(x, i),
                  LATERAL ts_debug('simple', u.x) WITH ORDINALITY AS d(alias, description, token, dictionaries,
                  dictionary, lexemes, o) ORDER BY u.i, d.o""", (corpus,))
    want = {}
    for i, alias, tok in pg.fetchall():
        want.setdefault(i - 1, []).append((alias, tok))
    bad = [(s, want.get(i, []), list(T.tokens(s))) for i, s in enumerate(corpus) if list(T.tokens(s)) != want.get(i, [])]
    assert not bad, "%d of %d differ; first: %r" % (len(bad), len(corpus), bad[:3])


def test_every_lexeme_set_matches_postgres_to_tsvector(pg):
    corpus = _fuzz(3000, 2026)
    pg.execute("SELECT tsvector_to_array(to_tsvector('simple', x)) FROM unnest(%s::text[]) WITH ORDINALITY u(x, i) "
               "ORDER BY i", (corpus,))
    got = [set(r[0]) for r in pg.fetchall()]
    bad = [(s, w, T.search_set(s)) for s, w in zip(corpus, got) if T.search_set(s) != w]
    assert not bad, "%d of %d differ; first: %r" % (len(bad), len(corpus), bad[:3])


def test_the_search_verdict_matches_postgres(pg):
    r = random.Random(77)
    contents = _fuzz(1500, 5)
    pairs = []
    for c in contents:
        q = r.choice([c, r.choice(contents), " ".join(r.sample(FRAG, 2)), r.choice(FRAG)])
        pairs.append((c, q))
    pg.execute("SELECT to_tsvector('simple', c) @@ plainto_tsquery('simple', q) FROM unnest(%s::text[], %s::text[]) "
               "WITH ORDINALITY u(c, q, i) ORDER BY i", ([p[0] for p in pairs], [p[1] for p in pairs]))
    want = [bool(x[0]) for x in pg.fetchall()]

    def ours(c, q):
        need = T.query_lexemes(q)
        return bool(need) and need <= T.search_set(c)
    bad = [(c, q, w) for (c, q), w in zip(pairs, want) if ours(c, q) != w]
    assert sum(want) > 100, "the corpus must contain real matches, or the check proves little"
    assert not bad, "%d of %d verdicts differ; first: %r" % (len(bad), len(pairs), bad[:3])


def test_the_fast_path_equals_the_full_parser():
    for s in _fuzz(4000, 31337) + [w.replace("<", "") .replace("&", "") for w in _fuzz(2000, 8)]:
        assert T.lexemes(s) == T._lexemes_full(s), (repr(s), T.lexemes(s), T._lexemes_full(s))
