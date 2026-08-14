"""The search load balancer, and the outgoing-proxy block the app writes for its bundled SearXNG.

Two features that answer the same problem from opposite ends: the scarce resource in a web search is
not compute, it is an IP address that engines still answer. `searxng_proxy_engines` changes WHICH
address a node's engine requests leave from (Tor1 → Tor2 → direct); `searxng_load_balance` changes
WHICH NODE asks. Both are pure plumbing around a network call, which is exactly the shape that looks
fine in review and fails in one specific arrangement — so the arrangements are enumerated here.
"""
import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------------------------
# The balancer
# --------------------------------------------------------------------------------------------
class FakeService:
    """Stands in for SearchService: records calls, and answers however the test wants."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    async def web_search_local(self, query, **kw):
        self.calls += 1
        a = self.answers.pop(0) if self.answers else []
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture()
def factory(monkeypatch):
    from app.services import search_factory
    monkeypatch.setattr(search_factory, "_rr_index", 0, raising=False)
    return search_factory


def _run(coro):
    return asyncio.run(coro)


def test_local_only_when_there_are_no_peers(factory, monkeypatch):
    monkeypatch.setattr(factory, "peers", lambda: [])
    monkeypatch.setattr(factory, "enabled", lambda: True)
    svc = FakeService([[{"url": "a"}]])
    out = _run(factory.web_search(svc, "q"))
    assert out == [{"url": "a"}] and svc.calls == 1


def test_a_node_that_raises_fails_over_to_the_next(factory, monkeypatch):
    """The whole point of a balancer. A node that cannot search must not end the search."""
    monkeypatch.setattr(factory, "enabled", lambda: True)
    monkeypatch.setattr(factory, "peers", lambda: ["http://peer-a"])
    seen = []

    async def fake_node(url, payload, timeout):
        seen.append(url)
        return [{"url": "from-peer"}]

    monkeypatch.setattr(factory, "_search_on_node", fake_node)
    svc = FakeService([RuntimeError("searxng down")])
    out = _run(factory.web_search(svc, "q"))
    assert out == [{"url": "from-peer"}], "a failing local node ended the search"
    assert seen == ["http://peer-a"]


def test_every_node_failing_is_an_empty_answer_not_an_exception(factory, monkeypatch):
    """Callers have always been able to treat [] as 'nothing found'. That contract survives."""
    monkeypatch.setattr(factory, "enabled", lambda: True)
    monkeypatch.setattr(factory, "peers", lambda: ["http://peer-a"])

    async def boom(url, payload, timeout):
        raise RuntimeError("peer down")

    monkeypatch.setattr(factory, "_search_on_node", boom)
    svc = FakeService([RuntimeError("local down")])
    assert _run(factory.web_search(svc, "q")) == []


def test_one_empty_answer_is_retried_and_two_are_believed(factory, monkeypatch):
    """A node whose engines are all suspended returns [] — and so does an obscure query.

    They are indistinguishable from here, so the first empty is retried elsewhere and the second is
    taken as the answer. Without the retry a single throttled node reports 'no results' for a query
    the other node can answer; without the stop it would walk every node on every genuinely empty
    search.
    """
    monkeypatch.setattr(factory, "enabled", lambda: True)
    monkeypatch.setattr(factory, "peers", lambda: ["http://peer-a"])
    asked = []

    async def fake_node(url, payload, timeout):
        asked.append(url)
        return [{"url": "peer-had-it"}]

    monkeypatch.setattr(factory, "_search_on_node", fake_node)
    svc = FakeService([[]])                                   # local answers, with nothing
    assert _run(factory.web_search(svc, "q")) == [{"url": "peer-had-it"}]
    assert asked == ["http://peer-a"], "an empty answer was taken at face value"

    # ...and when the second node is empty too, that IS the answer.
    svc2 = FakeService([[]])

    async def also_empty(url, payload, timeout):
        return []

    monkeypatch.setattr(factory, "_search_on_node", also_empty)
    assert _run(factory.web_search(svc2, "q")) == []


def test_the_rotation_starts_somewhere_new_each_time(factory, monkeypatch):
    """Otherwise every search starts on the same node and the balancer balances nothing."""
    monkeypatch.setattr(factory, "enabled", lambda: True)
    monkeypatch.setattr(factory, "peers", lambda: ["http://a", "http://b"])
    starts = []

    async def fake_node(url, payload, timeout):
        starts.append(url)
        return [{"url": url}]

    monkeypatch.setattr(factory, "_search_on_node", fake_node)
    for _ in range(3):
        _run(factory.web_search(FakeService([[{"url": "local"}]]), "q"))
    firsts = _run(factory._rotated([factory._LOCAL, "http://a", "http://b"]))
    assert len(set(firsts)) == 3          # the rotation is a permutation, not a fixed order
    assert starts, "peers were never reached across three searches"


def test_the_balancer_can_be_turned_off(factory, monkeypatch):
    monkeypatch.setattr(factory, "enabled", lambda: False)
    monkeypatch.setattr(factory, "peers", lambda: ["http://peer-a"])

    async def never(url, payload, timeout):
        raise AssertionError("a peer was called with load balancing off")

    monkeypatch.setattr(factory, "_search_on_node", never)
    assert _run(factory.web_search(FakeService([[{"url": "local"}]]), "q")) == [{"url": "local"}]


def test_this_node_is_not_in_its_own_peer_list(factory):
    """`_LOCAL` already represents it. Left in, a node forwards searches to itself over HTTP and
    starves the real peers in the rotation."""
    import app.services.load_balancer as lb
    assert factory.parse_search_server_urls("") == []
    # exclude_self is what does it; assert the factory asks for it rather than re-deriving the rule.
    src = open(os.path.join(ROOT, "app/services/search_factory.py"), encoding="utf-8").read()
    assert "exclude_self=True" in src


# --------------------------------------------------------------------------------------------
# The loop guard
# --------------------------------------------------------------------------------------------
def test_the_peer_endpoint_never_calls_the_balanced_entry_point():
    """A→B must not be able to become B→A→B.

    The guard is structural — `/api/search` calls `web_search_local`, which never forwards — so it is
    checked structurally. A header hop-count could not do this job: the caller writes the headers.
    """
    src = open(os.path.join(ROOT, "app/routers/search_api.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    called = {getattr(n.func, "attr", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "web_search_local" in called, "the peer endpoint no longer calls the local-only search"
    assert "web_search" not in called, (
        "the peer endpoint calls the BALANCED web_search — node A asking node B would make node B "
        "ask node A, and so on")


def test_web_search_local_does_not_forward():
    """The other half of the same guard, on the service side.

    Parsed, not grepped: the method's own docstring and comments explain the loop it must not create,
    so a text search for the name matches the explanation and fails on correct code.
    """
    import inspect
    import textwrap
    from app.services.search_service import SearchService
    tree = ast.parse(textwrap.dedent(inspect.getsource(SearchService.web_search_local)))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):                       # `from app.services import search_factory`
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {a.name for a in node.names}
    assert "search_factory" not in names, "web_search_local forwards — that is the loop"


# --------------------------------------------------------------------------------------------
# The outgoing-proxy block
# --------------------------------------------------------------------------------------------
BASE_YML = "use_default_settings: true\n\nserver:\n  secret_key: \"x\"\n"


def _write(tmp_path, text):
    p = tmp_path / "settings.yml"
    p.write_text(text, encoding="utf-8")
    return p


def test_the_block_is_written_toggled_and_idempotent(tmp_path, monkeypatch):
    from app.services import searxng_native as sn
    p = _write(tmp_path, BASE_YML)
    monkeypatch.setattr(sn, "settings_path", lambda: p)

    monkeypatch.setattr(sn, "_proxy_wanted", lambda: (True, "http://127.0.0.1:8119"))
    sn.apply_outgoing_proxy()
    once = p.read_text()
    assert "outgoing:" in once and "http://127.0.0.1:8119" in once
    assert "request_timeout: 12.0" in once, "the Tor timeout has to ride with the proxy"
    sn.apply_outgoing_proxy()
    assert p.read_text() == once, "rewriting the block changed the file a second time"

    monkeypatch.setattr(sn, "_proxy_wanted", lambda: (False, "http://127.0.0.1:8119"))
    sn.apply_outgoing_proxy()
    off = p.read_text()
    assert "\noutgoing:" not in off, "turning it off left a live outgoing block"
    assert sn._PROXY_BEGIN in off and sn._PROXY_END in off

    monkeypatch.setattr(sn, "_proxy_wanted", lambda: (True, "http://127.0.0.1:8119"))
    sn.apply_outgoing_proxy()
    assert p.read_text() == once, "off then on did not return to the same file"


def test_the_legacy_commented_stub_is_adopted_not_duplicated(tmp_path, monkeypatch):
    """Every node installed before this shipped has that stub at the end of its settings file."""
    from app.services import searxng_native as sn
    legacy = (BASE_YML + "\n# outgoing:\n#   request_timeout: 12.0\n#   proxies:\n"
              "#     all://:\n#       - http://127.0.0.1:8119\n")
    p = _write(tmp_path, legacy)
    monkeypatch.setattr(sn, "settings_path", lambda: p)
    monkeypatch.setattr(sn, "_proxy_wanted", lambda: (True, "http://127.0.0.1:8119"))
    sn.apply_outgoing_proxy()
    text = p.read_text()
    assert text.count("outgoing:") == 1, "the managed block was appended beside the old stub"


def test_an_operator_written_block_is_left_alone(tmp_path, monkeypatch):
    """Their file, their proxy. Silently moving every engine request is not ours to do."""
    from app.services import searxng_native as sn
    mine = BASE_YML + "\noutgoing:\n  proxies:\n    all://:\n      - http://10.0.0.9:3128\n"
    p = _write(tmp_path, mine)
    monkeypatch.setattr(sn, "settings_path", lambda: p)
    monkeypatch.setattr(sn, "_proxy_wanted", lambda: (True, "http://127.0.0.1:8119"))
    assert sn.apply_outgoing_proxy() == "operator-managed"
    assert p.read_text() == mine


def test_unreadable_settings_leave_the_file_untouched(tmp_path, monkeypatch):
    """No settings store (a bare `python -m`, an early boot) is not a licence to invent a policy."""
    from app.services import searxng_native as sn
    p = _write(tmp_path, BASE_YML)
    monkeypatch.setattr(sn, "settings_path", lambda: p)
    monkeypatch.setattr(sn, "_proxy_wanted", lambda: (False, ""))
    sn.apply_outgoing_proxy()
    assert p.read_text() == BASE_YML


def test_a_missing_or_unwritable_file_is_not_fatal(tmp_path, monkeypatch):
    """Search with whatever the file already says beats a node that cannot search at all."""
    from app.services import searxng_native as sn
    monkeypatch.setattr(sn, "settings_path", lambda: tmp_path / "nope.yml")
    assert sn.apply_outgoing_proxy() == "no settings file"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
