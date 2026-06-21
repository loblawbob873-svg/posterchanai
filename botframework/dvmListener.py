"""Nostr NIP-90 Data Vending Machine listener (`--dvm` mode).

Subscribes to job-request events (kind 5xxx) on the bot's relays and fulfils the ones this node
supports using the local AI, publishing a result (kind 6xxx = request kind + 1000) plus job-feedback
(kind 7000). v1 handles TEXT jobs — text generation + summarization — via the same `generate_reply`
the chat bot uses. Signed with the bot's own key. Dormant unless an admin creates a DVM bot.

NIP-90 refs: request kinds 5000–5999, results 6000–6999, feedback 7000. We read the prompt from the
job's `i` (input) tags, falling back to the event content.
"""
import os
import json
import time

import nostr as _nk
from ai import generate_reply, is_ai_configured
from app.services.nostr import event as _ev

# Job kinds we fulfil → human label. Result kind is always request kind + 1000.
_SUPPORTED = {
    5050: "text-generation",
    5001: "summarization",
    5000: "text",
}
# Kinds whose prompt should be summarized rather than answered.
_SUMMARIZE = {5000, 5001}

# Cap jobs processed per poll so a flood can't monopolise the GPU (each runs under the app's GPU lock).
_MAX_PER_POLL = int(os.getenv("DVM_MAX_PER_POLL", "3"))

_SEEN: set = set()   # request ids handled this run (dedupe)
_since = None        # query cursor (unix seconds)


def _input_text(ev: dict) -> str:
    """NIP-90 inputs live in `i` tags (['i', <data>, <type>, …]); fall back to content."""
    parts = [t[1] for t in ev.get("tags", []) if t and t[0] == "i" and len(t) >= 2 and t[1]]
    return ("\n\n".join(parts).strip()) or (ev.get("content") or "").strip()


def _publish(kind: int, content: str, tags: list):
    ev = _ev.build_event(_nk._SECKEY, kind, content, tags=tags)
    _nk._run(_nk._svc.relay.publish(_nk._RELAYS, ev))
    return ev


def _feedback(req: dict, status: str, extra: str = ""):
    """kind-7000 job feedback (processing/success/error) referencing the request + customer."""
    _publish(7000, extra, [["status", status], ["e", req["id"]], ["p", req["pubkey"]]])


def _result(req: dict, output: str):
    """kind-(req+1000) result, referencing the request + customer, echoing the input tags."""
    tags = [["e", req["id"]], ["p", req["pubkey"]]]
    tags += [t for t in req.get("tags", []) if t and t[0] == "i"]
    _publish(req["kind"] + 1000, output, tags)


def process_job_requests():
    if not is_ai_configured():
        print("[dvm] AI not configured — idle", flush=True)
        return
    if not _nk._SECKEY:
        print("[dvm] no NOSTR_NSEC — idle", flush=True)
        return
    global _since
    flt = {"kinds": sorted(_SUPPORTED), "limit": 50}
    if _since:
        flt["since"] = _since
    try:
        events = _nk._run(_nk._svc.relay.query(_nk._RELAYS, [flt])) or []
    except Exception as e:
        print(f"[dvm] query failed: {e}", flush=True)
        return
    _since = int(time.time()) - 5   # small overlap so we don't miss boundary events

    done = 0
    for ev in sorted(events, key=lambda e: e.get("created_at", 0)):
        rid = ev.get("id")
        if not rid or rid in _SEEN or ev.get("pubkey") == _nk._PUBKEY:
            continue
        _SEEN.add(rid)
        if ev.get("kind") not in _SUPPORTED:
            continue
        if done >= _MAX_PER_POLL:
            break
        prompt = _input_text(ev)
        if not prompt:
            _feedback(ev, "error", "no input provided")
            continue
        done += 1
        try:
            _feedback(ev, "processing")
            if ev["kind"] in _SUMMARIZE:
                prompt = "Summarize the following clearly and concisely:\n\n" + prompt
            output = (generate_reply(prompt, thread_history=None, ping=False) or "").strip()
            if not output:
                _feedback(ev, "error", "empty result")
                continue
            _result(ev, output)
            _feedback(ev, "success")
            print(f"[dvm] fulfilled kind-{ev['kind']} job {rid[:12]} for {ev.get('pubkey','')[:12]}", flush=True)
        except Exception as e:
            print(f"[dvm] job {rid[:12]} failed: {e}", flush=True)
            try:
                _feedback(ev, "error", str(e)[:200])
            except Exception:
                pass

    if len(_SEEN) > 5000:
        _SEEN.clear()
