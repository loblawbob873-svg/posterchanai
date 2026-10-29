#!/usr/bin/env python3
"""Ask THIS NODE'S MODEL the desktop assistant's questions and score the plans it makes.

The unit tests (tests/test_desktop_assist.py) prove the parser keeps only real steps; they cannot say whether the
node's own small model, given the shipped prompt, plans the RIGHT steps. This sends each request below through the
shipped desktop_assist.build_messages, the node's /v1 endpoint, and the shipped parse_steps -- exactly production's
path minus the HTTP route -- and checks the surviving steps against what a person meant.

    PC_API_KEY=<key> venv-unified/bin/python scripts/audit_desktop_assistant.py [--base http://127.0.0.1:3051]

One line per case: PASS / FAIL, the steps kept, and (on FAIL) what was expected. Exit 1 when any case fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services import desktop_assist as D  # noqa: E402

APPS = [{"view": v, "label": l} for v, l in (
    ("global", "Social"), ("messages", "Messages"), ("notes", "Notes"), ("calendar", "Calendar"), ("music", "Music"),
    ("settings", "Settings"), ("terminal", "Terminal"), ("photos", "Photos"), ("recorder", "Screen Recorder"),
    ("ai", "PosterChan AI"), ("mail", "Email"), ("concord", "Communities"))]
WINDOWS = [{"title": "Social", "view": "global", "ws": 1, "focused": True},
           {"title": "Terminal", "view": "terminal", "ws": 1, "focused": False}]


def has(kind, **want):
    def check(steps):
        return any(s["do"] == kind and all(s.get(k) == v for k, v in want.items()) for s in steps)
    check.__doc__ = f"{kind} {want}"
    return check


def none_of(*kinds):
    def check(steps):
        return not any(s["do"] in kinds for s in steps)
    check.__doc__ = f"no {kinds}"
    return check


CASES = [
    ("open my notes", [has("open", view="notes")]),
    ("put social on the left and the terminal on the right", [has("snap", win=1, zone="left"), has("snap", win=2, zone="right")]),
    ("open calendar and notes side by side",
     [has("open", view="calendar"), has("open", view="notes"), has("snap", zone="left"), has("snap", zone="right")]),
    ("tile all my windows", [has("arrange")]),
    ("mute the music", [has("volume", app="music", mute=True)]),
    ("turn social down to 20 percent", [has("volume", app="global", level=20)]),
    ("take a screenshot of part of the screen", [has("screenshot", mode="region")]),
    ("record my screen", [lambda st: has("record")(st) or has("open", view="recorder")(st)]),
    ("make a note: buy milk and eggs tomorrow", [has("note")]),
    ("draft a post saying gm nostr", [has("post")]),
    ("search nostr for gentoo", [has("search", text="gentoo")]),
    ("move the terminal to workspace 2", [has("move", win=2, n=2)]),
    ("close the terminal", [has("close", win=2)]),
    ("delete all my files", [none_of("close", "post", "note")]),          # nothing in the catalogue does that
    ("what's the weather like", [lambda s: len(s) == 0]),
]


MODEL = os.environ.get("PC_MODEL", "Qwen3.5-9B-abliterated-Q4_K_M.gguf")   # the node's chat model (/v1/models)


def ask(base, key, messages):
    body = json.dumps({"model": MODEL, "messages": messages, "temperature": 0.1, "max_tokens": 900}).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", body,
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.load(r)["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:3051")
    a = ap.parse_args()
    key = os.environ.get("PC_API_KEY", "")
    if not key:
        raise SystemExit("set PC_API_KEY (an API key for this node's /v1)")
    apps, wins = D.clean_apps(APPS), D.clean_windows(WINDOWS)
    failed = 0
    for q, checks in CASES:
        plan = D.parse_steps(ask(a.base, key, D.build_messages(q, apps, wins, 1, "2026-10-28")), apps, wins, 1)
        bad = [c.__doc__ or "no steps" for c in checks if not c(plan["steps"])]
        failed += bool(bad)
        steps = "; ".join(json.dumps({k: v for k, v in s.items() if k != "label"}) for s in plan["steps"]) or "(none)"
        print(("FAIL" if bad else "PASS"), f"{q!r:52} -> {steps}" + (f"   expected: {bad}" if bad else ""))
    print(f"\n{len(CASES) - failed}/{len(CASES)} plans did what was asked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
