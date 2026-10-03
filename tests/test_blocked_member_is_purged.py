"""An account an ADMIN blocked is purged even when it is registered here; a bridge HEURISTIC still is not.

"run relay purge, we need to make sure that blocked users can't do anything on the platform despite
having a nip05 in their profile". "Purge now" removed 646 events on 2026-10-02 and none of the blocked
member's: his git issue, posts and lists stayed served, because `delete_pubkeys` spared every account in
the preserve set (registered users) -- a rule written for members MIS-flagged as bridges by the
cross-post heuristic, and applied to the admin's explicit blocklist too.
"""
import ast
from pathlib import Path

from tests.test_relay_prune import _ev, _run, store_factory  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
MEMBER = "b" * 64


def test_an_explicitly_blocked_member_is_purged(store_factory):  # noqa: F811
    async def go(loop):
        store = store_factory(loop)
        await store.add_events_bulk([_ev(i, kind=k, pubkey=MEMBER) for i, k in ((1, 1), (2, 1621), (3, 0), (4, 10000))],
                                    origin="direct")
        await store.add_events_bulk([_ev(i) for i in range(100, 105)])
        store.set_preserve_pubkeys([MEMBER])
        # The bridge heuristic's call: a registered member it flags keeps their history.
        assert await store.delete_pubkeys([MEMBER]) == 0
        assert await store.count() == 9
        # The admin named him.
        assert await store.delete_pubkeys([MEMBER], spare_preserved=False) == 4
        assert await store.count() == 5, "somebody else's events went with him"
    _run(go)


def _calls(src, name):
    tree = ast.parse(src)
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == name]


def test_the_blocklist_and_delete_author_purge_registered_accounts_and_the_bridge_heuristic_does_not():
    src = (ROOT / "app/services/nostr_relay/thread.py").read_text()
    calls = _calls(src, "delete_pubkeys")
    def spares(c):
        kw = {k.arg: k.value for k in c.keywords}
        return not (isinstance(kw.get("spare_preserved"), ast.Constant) and kw["spare_preserved"].value is False)
    by_arg = {ast.unparse(c.args[0]): spares(c) for c in calls}
    assert by_arg.get("fresh['blocked_pubkeys']") is False, "the blocklist purge still spares registered accounts"
    assert by_arg.get("pks") is False, "delete-author still spares registered accounts"
    assert by_arg.get("list(pks)") is True, "the bridge heuristic must keep sparing members it mis-flags"
