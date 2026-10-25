"""Every route that runs the node's MODEL (or another GPU/CPU-heavy helper) for a caller who is not signed in is
bounded by PublicWorkMiddleware -- body size, two at a time, 60 a minute per address.

`/client/emoji-suggest` called the LLM for anybody with none of those bounds (2026-10-24 auth scan), while its
neighbours summarize/hashtags/translate were bounded. The list is DISCOVERED: walk every route, find the ones with no
user gate in their dependency tree whose handler runs inference, and require each to be in PublicWorkMiddleware.LIMITS.
"""
import inspect
import re

from fastapi.routing import APIRoute, _IncludedRouter

from app.middleware.public_work import PublicWorkMiddleware

# What "runs the model / heavy work" looks like in a handler.
HEAVY = re.compile(r"get_inference_service|chat_completion\(|voice_factory|tts_service|stt_service|"
                   r"transcribe\(|media_service\.compress|_render_post_card_png")
# What a user gate looks like in a dependency tree.
GATE = re.compile(r"get_current_user$|get_admin_user|get_ai_user|get_instance_user|_member|get_bot_auth|"
                  r"get_torrent_user|get_media_admin|_viewer$|^get_\w+_auth$")   # get_voice_auth, get_image_auth: key/JWT/peer


def _flat(routes):
    for r in routes:
        if isinstance(r, _IncludedRouter):
            for c in r.effective_candidates():
                yield from (_flat([c]) if isinstance(c, _IncludedRouter) else [c])
        else:
            yield r


def _calls(d):
    out = []
    for s in d.dependencies:
        out.append(getattr(s.call, "__name__", ""))
        out += _calls(s)
    return out


def _public_heavy():
    from app.main import app
    out = []
    for c in _flat(app.router.routes):
        o = getattr(c, "original_route", c)
        if not isinstance(o, APIRoute) or "POST" not in (o.methods or ()):
            continue
        if any(GATE.search(n) for n in _calls(getattr(c, "dependant", None) or o.dependant)):
            continue
        try:
            src = inspect.getsource(o.endpoint)
        except (OSError, TypeError):
            continue
        if HEAVY.search(src) and not re.search(r"_verify_self_auth|verify_nip98|_verify_admin", src):
            out.append(c.path)
    return sorted(set(out))


def test_the_public_heavy_routes_are_found():
    found = _public_heavy()
    assert "/client/summarize" in found and "/client/emoji-suggest" in found, found


def test_every_public_route_that_runs_the_model_is_bounded():
    unbounded = [p for p in _public_heavy() if p not in PublicWorkMiddleware.LIMITS]
    assert not unbounded, ("anybody can queue model/heavy work here without PublicWorkMiddleware's bounds", unbounded)
