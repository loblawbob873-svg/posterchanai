"""THE SELF-SERVE GATE IS A PRIVILEGE-ESCALATION FIX, AND IT HAD NO TEST.

`fedi_bridge_access.py` had ZERO test references. From `enable`'s own docstring:

    This function spends the OPERATOR's admin token: it creates an account on the home instance,
    force-confirms + approves it (bypassing that instance's manual approval), mints a
    read/write/follow token and turns on cross-posting. Two endpoints reach it — client.py's is
    admin-authenticated, auth.py's needed only a session, and /api/auth/nostr-login mints a session
    for ANY npub that can sign a challenge. So any passer-by could provision themselves an account
    on the operator's instance and federate through it.

Anyone can mint a Nostr keypair, so "can sign a challenge" is not an identity check — it is a
formality. The hole was closed by gating on `fedi_bridge_self_serve` (default OFF) inside the
SERVICE rather than the router, deliberately, "so no future caller can reintroduce the hole".

That reasoning only holds while the gate is actually in the service. Nothing was checking that it
still is, that it still defaults to closed, or — the part that is easy to get wrong while
"tidying" — that it is checked BEFORE the operator's admin token is spent. A gate that refuses
AFTER creating the account has not refused anything.
"""
import asyncio

import pytest

from app.services import fedi_bridge_access as access


class _User:
    """Enough of a User to get past the gate and no further."""
    def __init__(self):
        self.nostr_npub = None
        self.pleroma_enabled = False
        self.pleroma_access_token = None
        self.pleroma_instance_url = None


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def spent(monkeypatch):
    """Records every use of the operator's credentials. Nothing here may be touched on a refusal."""
    calls = []
    monkeypatch.setattr(access, "_home_instance",
                        lambda: calls.append("home_instance") or "https://pleroma.example")
    monkeypatch.setattr(access, "_admin_token",
                        lambda: calls.append("admin_token") or "operator-admin-token")
    return calls


def _settings(monkeypatch, **kv):
    monkeypatch.setattr(access.settings_store, "get",
                        lambda key, default="": kv.get(key, default))


# --------------------------------------------------------------------------- closed by default


def test_self_serve_is_refused_when_the_setting_is_unset(monkeypatch, spent):
    """The default. An unset setting must read as OFF — this is the state every node ships in."""
    _settings(monkeypatch)
    out = _run(access.enable(None, _User(), by_admin=False))
    assert out["ok"] is False
    assert "admin" in out["error"].lower()


def test_the_refusal_happens_before_the_operator_token_is_spent(monkeypatch, spent):
    """The half that is easy to lose while refactoring. `enable` force-confirms and approves an
    account on the operator's instance; a gate that runs after that has already been escalated
    past. Refusing must cost nothing."""
    _settings(monkeypatch)
    _run(access.enable(None, _User(), by_admin=False))
    assert spent == [], \
        "the operator's instance/admin token was reached on a REFUSED self-serve enable: %r" % spent


@pytest.mark.parametrize("value", ["", " ", "0", "false", "no", "off", "nope", "None", "null", "2"])
def test_only_an_explicit_affirmative_opens_the_gate(monkeypatch, spent, value):
    """A stray value must fail CLOSED. `get_bool`-style truthiness that treats any non-empty string
    as true would open this on a setting somebody typed "no" into."""
    _settings(monkeypatch, fedi_bridge_self_serve=value)
    out = _run(access.enable(None, _User(), by_admin=False))
    assert out["ok"] is False, f"{value!r} opened the self-serve gate"
    assert spent == []


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "  On  ", "Yes"])
def test_an_operator_who_opts_in_is_let_through(monkeypatch, spent, value):
    """The feature still has to work when it is deliberately turned on, or the fix is a removal.
    Getting PAST the gate is proven by reaching the configuration check beyond it."""
    _settings(monkeypatch, fedi_bridge_self_serve=value)
    out = _run(access.enable(None, _User(), by_admin=False))
    assert out["ok"] is False              # no linked Nostr key on this fake user
    assert "admin" not in out["error"].lower(), \
        f"{value!r} was refused by the gate instead of being let through"
    assert spent, "the gate was passed but the home instance was never consulted"


# --------------------------------------------------------------------------- the admin path


def test_an_admin_grant_is_never_gated(monkeypatch, spent):
    """`by_admin=True` is the client.py path, which is already admin-authenticated. If the setting
    ever gated that too, the admin's own Grant button would stop working on every default node —
    and the only way to fix it would be to turn the insecure setting on."""
    _settings(monkeypatch)                                   # self-serve OFF
    out = _run(access.enable(None, _User(), by_admin=True))
    assert "admin" not in out["error"].lower()
    assert spent, "an admin grant did not reach the home instance"


def test_by_admin_defaults_to_false(monkeypatch, spent):
    """The signature is the gate. A caller that forgets the argument must get the SAFE branch —
    which is what makes 'no future caller can reintroduce the hole' true."""
    _settings(monkeypatch)
    out = _run(access.enable(None, _User()))                 # no by_admin passed at all
    assert out["ok"] is False and "admin" in out["error"].lower()
    assert spent == []


# --------------------------------------------------------------------------- the gate is in the service


def test_the_gate_lives_in_the_service_not_the_router():
    """Stated as the reason it was put here. If it migrates back into a router, the OTHER router
    reaching `enable` is unguarded again — which is exactly how the hole existed the first time."""
    src = (access.__file__ or "")
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "fedi_bridge_self_serve" in body, \
        "the self-serve gate is no longer in fedi_bridge_access — every caller is unguarded"
    gate = body.index("fedi_bridge_self_serve")
    assert body.index("async def enable") < gate, "the gate is not inside enable()"


def _enable_call_sites():
    """Every `fedi_bridge_access.enable(...)` in a router, with the function that contains it.

    Parsed, not grepped: the first version of this test matched a COMMENT in social_login.py and
    reported it as an unguarded caller."""
    import ast
    import pathlib
    root = pathlib.Path(access.__file__).resolve().parents[2]
    sites = []
    for path in sorted((root / "app" / "routers").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not (isinstance(f, ast.Attribute) and f.attr == "enable"
                        and isinstance(f.value, ast.Name) and f.value.id == "fedi_bridge_access"):
                    continue
                kw = {k.arg: k.value for k in node.keywords}
                sites.append((path.name, fn, node.lineno, kw))
    return sites


def test_enable_is_actually_reached_from_the_routers():
    """Everything below is a sweep over these call sites. If the sweep set is empty — the module
    was renamed, the import moved — every test under it passes by inspecting nothing."""
    assert _enable_call_sites(), \
        "no fedi_bridge_access.enable() call sites found; the guards below are inspecting nothing"


def test_by_admin_is_never_taken_from_the_request():
    """`by_admin=True` bypasses the gate completely, so it must be a decision the SERVER makes.
    Wired to anything the caller sends — a field on the request body, a header, a query parameter —
    the gate becomes a checkbox the attacker ticks, which is the original hole with extra steps."""
    import ast
    offenders = []
    for name, _fn, lineno, kw in _enable_call_sites():
        val = kw.get("by_admin")
        if val is None:
            continue                                   # omitted = the safe, gated branch
        if not isinstance(val, ast.Constant):
            offenders.append(f"{name}:{lineno}: by_admin={ast.unparse(val)}")
    assert offenders == [], (
        "by_admin must be a literal decided by the server, not a value the request can steer:\n"
        + "\n".join(offenders)
    )


def test_a_caller_that_bypasses_the_gate_verifies_the_admin_first():
    """`by_admin=True` is only sound because the route carrying it is admin-authenticated. This
    pins the pairing, so a new route cannot copy the convenient half of client.py's line without
    the `_verify_admin_auth` above it."""
    import ast
    offenders = []
    for name, fn, lineno, kw in _enable_call_sites():
        val = kw.get("by_admin")
        if not (isinstance(val, ast.Constant) and val.value is True):
            continue
        body = ast.unparse(fn)
        if not any(marker in body for marker in
                   ("_verify_admin_auth", "is_admin", "require_admin", "admin_required")):
            offenders.append(f"{name}:{lineno} (in {fn.name})")
    assert offenders == [], (
        "these bypass the self-serve gate without checking that the caller is an admin:\n"
        + "\n".join(offenders)
    )


def test_the_session_only_route_still_takes_the_gated_branch():
    """auth.py's /bridge-access needs only a session, and /api/auth/nostr-login mints one for any
    npub that can sign a challenge — which anybody can, because anybody can make a keypair. That
    route omitting `by_admin` IS the fix. Adding `by_admin=True` there would restore the hole
    exactly, and would read like a bug fix while doing it."""
    import ast
    sites = [s for s in _enable_call_sites() if s[0] == "auth.py"]
    assert sites, "auth.py no longer calls enable() — if the route moved, move this test with it"
    for name, _fn, lineno, kw in sites:
        val = kw.get("by_admin")
        assert val is None or (isinstance(val, ast.Constant) and val.value is False), \
            f"{name}:{lineno} bypasses the self-serve gate from a session-only route"
