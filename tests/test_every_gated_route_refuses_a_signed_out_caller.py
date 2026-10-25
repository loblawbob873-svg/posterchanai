"""EVERY route whose dependencies carry a user gate refuses a caller who is not signed in -- and the list is DISCOVERED.

tests/test_every_admin_route_refuses_non_admins.py does this for ADMIN routes. The 2026-10-24 route-coverage scan
found ~440 routes behind a user gate (a session, instance membership, AI access, the torrent/search/media gates, an
API key) and no test asking the plain question: does a signed-out request get 401/403, before the handler runs?

Same hermetic world (its fixtures are imported): no network, no database, every handler body replaced by a 418
sentinel. So a 418 here means the gate let an anonymous caller through to the handler. The peer-node header WITHOUT
the shared secret must be refused the same way -- that header alone is settable by anybody (app/utils/lb_auth.py).
"""
import re
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

from app.utils import lb_auth
from tests.test_every_admin_route_refuses_non_admins import (  # noqa: F401  (fixtures)
    SENTINEL, _as, _call, _flat, app_and_routes, world)

# A REQUIRED user gate: raises without credentials (the *_optional readers return None and are not gates).
GATE = re.compile(r"^(get_current_user|get_admin_user|get_ai_user|get_instance_user|get_torrent_user|"
                  r"get_search_member|get_media_admin|get_media_user|get_bot_auth|get_\w+_auth|require_\w+)$")


def _names(dependant):
    out = []
    for s in dependant.dependencies:
        out.append(getattr(s.call, "__name__", ""))
        out += _names(s)
    return out


def _gated_rows():
    from app.main import app
    rows = []
    for c in _flat(app.router.routes):
        o = getattr(c, "original_route", c)
        if not isinstance(o, APIRoute):
            continue
        names = _names(getattr(c, "dependant", None) or o.dependant)
        gates = sorted({n for n in names if GATE.match(n)})
        if not gates:
            continue
        for m in sorted(o.methods or ()):
            if m not in ("HEAD", "OPTIONS"):
                rows.append(SimpleNamespace(method=m, path=c.path, gates=gates))
    return rows


GATED = _gated_rows()


def test_the_gated_routes_are_found():
    paths = {r.path for r in GATED}
    for must in ("/api/torrent/remove", "/api/storage/save-avatar", "/api/mail/move", "/api/proxy-image",
                 "/api/media-center/{library_id}/move-folder"):
        assert must in paths, ("a route that is gated was not discovered", must)
    assert len(GATED) > 300, len(GATED)


@pytest.mark.parametrize("row", GATED, ids=lambda r: "%s %s" % (r.method, r.path))
def test_a_signed_out_caller_is_refused_before_the_handler(world, row):
    _as(world, None)
    r = _call(world, row)
    assert r.status_code != SENTINEL, ("a signed-out caller reached the handler", row.method, row.path, row.gates)
    assert r.status_code in (401, 403, 404, 422, 429, 503), (row.method, row.path, r.status_code, r.text[:160])


@pytest.mark.parametrize("row", GATED, ids=lambda r: "%s %s" % (r.method, r.path))
def test_the_bare_peer_node_header_is_not_a_credential(world, row, monkeypatch):
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: "the-real-secret")
    _as(world, None)
    r = _call(world, row, headers={lb_auth.FLAG_HEADER_NAME: "true"})
    assert r.status_code != SENTINEL, ("the bare load-balancer header let a caller through", row.method, row.path)
