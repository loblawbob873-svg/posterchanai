"""✨ "Do it" in the command bar: the DESKTOP assistant -- a request in words becomes desktop actions the person runs.

Asked 2026-10-28 ("AI desktop assistant"). The window ✨ panel already acts INSIDE one window; this one acts on the
desktop itself: open apps, put windows side by side or in thirds, workspaces, an app's volume, a screenshot, a
recording, a note, a post draft, a search. Same rule as the window panel: the model only PROPOSES. It answers with a
plan; this module keeps only steps from a fixed catalogue that name things the client said exist (an app in its
list, a window by its number, a real zone), and the client shows them as buttons the person presses. Nothing is
sent, deleted or posted by the assistant: "post" opens the composer with the text, unsent.

The model is this node's own (CommandService's chat model, via chat_assist_service._chat).
"""
import logging
import re

from app.services import chat_assist_service as svc

logger = logging.getLogger(__name__)

STEP_MAX = 6
APPS_MAX = 80
WINDOWS_MAX = 30
WORKSPACES_MAX = 9
ZONES = {"left": "left half", "right": "right half", "max": "full screen", "tl": "top-left quarter",
         "tr": "top-right quarter", "bl": "bottom-left quarter", "br": "bottom-right quarter", "l3": "left third",
         "m3": "middle third", "r3": "right third", "l23": "left two thirds", "r23": "right two thirds"}
ARRANGE = ("grid", "side-by-side")
SHOTS = ("screen", "region")
KINDS = ("open", "snap", "arrange", "workspace", "move", "volume", "screenshot", "record", "note", "post", "search",
         "close")
_VIEW = re.compile(r"^[a-z0-9_:-]{1,40}$")


def clean_apps(apps) -> list:
    out, seen = [], set()
    for a in list(apps or [])[:APPS_MAX * 2]:
        if not isinstance(a, dict):
            continue
        v = str(a.get("view") or "").strip()
        if _VIEW.match(v) and v not in seen:
            seen.add(v)
            out.append({"view": v, "label": svc._clean(a.get("label") or v, 40)})
        if len(out) >= APPS_MAX:
            break
    return out


def clean_windows(windows) -> list:
    out = []
    for w in list(windows or [])[:WINDOWS_MAX]:
        if not isinstance(w, dict):
            continue
        v = str(w.get("view") or "").strip()
        try:
            ws = max(1, min(WORKSPACES_MAX, int(w.get("ws") or 1)))
        except (TypeError, ValueError):
            ws = 1
        out.append({"title": svc._clean(w.get("title") or v, 60), "view": v if _VIEW.match(v) else "", "ws": ws,
                    "focused": bool(w.get("focused"))})
    return out


def build_messages(instruction: str, apps: list, windows: list, workspaces: int, today: str = "") -> list:
    zones = ", ".join(f'"{k}" ({v})' for k, v in ZONES.items())
    system = (
        "You are the PosterChan desktop's assistant. You act ONLY through buttons the user presses, so never claim "
        "anything was done. Reply with ONE JSON object and nothing else:\n"
        "{\"answer\": one short sentence saying what the buttons will do (or a question if the request is unclear),\n"
        " \"steps\": [{\"do\": kind, \"label\": 2-5 word button text, ...fields}]}\n"
        "Step kinds and their fields:\n"
        "- \"open\": open an app. \"view\": one of the app ids listed.\n"
        f"- \"snap\": put an open window in part of the screen. \"win\": window number, \"zone\": one of {zones}.\n"
        "- \"arrange\": arrange all open windows. \"mode\": \"grid\" or \"side-by-side\".\n"
        "- \"workspace\": go to workspace \"n\" (a number; one more than the last makes a new one).\n"
        "- \"move\": move window \"win\" to workspace \"n\".\n"
        "- \"volume\": set an app's volume. \"app\": an app id (or \"music\", or \"other\"), and \"level\" 0-100 or "
        "\"mute\": true/false.\n"
        "- \"screenshot\": \"mode\": \"screen\" or \"region\".\n"
        "- \"record\": open the screen recorder.\n"
        "- \"note\": save a new note. \"text\": its content (first line is the title).\n"
        "- \"post\": open the post composer with \"text\" filled in. It is NOT sent; the user reviews it.\n"
        "- \"search\": search Nostr for \"text\".\n"
        "- \"close\": close window \"win\".\n"
        "To open two apps side by side: open both, then snap one \"left\" and one \"right\" -- a window opened by an "
        "earlier step is numbered after the windows listed, in the order opened. Use ONLY the app ids and window "
        f"numbers listed; never invent one. At most {STEP_MAX} steps. If the request is not something these steps "
        "can do, say so in \"answer\" with [] steps. Never write placeholder text such as [your text]; ask instead. "
        "Match the user's language.\n"
        "Examples (with Notes and Settings among the apps, window 1 = Social):\n"
        "\"open notes\" -> {\"answer\": \"Opening Notes.\", \"steps\": [{\"do\": \"open\", \"view\": \"notes\", \"label\": \"Open Notes\"}]}\n"
        "\"notes and settings side by side\" -> {\"answer\": \"Notes left, Settings right.\", \"steps\": [{\"do\": \"open\", \"view\": \"notes\"}, "
        "{\"do\": \"open\", \"view\": \"settings\"}, {\"do\": \"snap\", \"win\": 2, \"zone\": \"left\"}, {\"do\": \"snap\", \"win\": 3, \"zone\": \"right\"}]}\n"
        "\"close social\" -> {\"answer\": \"Closing Social.\", \"steps\": [{\"do\": \"close\", \"win\": 1}]}\n"
        "\"mute the music\" -> {\"answer\": \"Muting Music.\", \"steps\": [{\"do\": \"volume\", \"app\": \"music\", \"mute\": true}]}\n"
        "\"note: call mum\" -> {\"answer\": \"Saving a note.\", \"steps\": [{\"do\": \"note\", \"text\": \"call mum\"}]}\n"
        "\"post hello world\" -> {\"answer\": \"Drafting the post for you to check.\", \"steps\": [{\"do\": \"post\", \"text\": \"hello world\"}]}\n"
        "\"send social to workspace 3\" -> {\"answer\": \"Moving Social to workspace 3.\", \"steps\": [{\"do\": \"move\", \"win\": 1, \"n\": 3}]}\n"
        "\"delete my photos\" -> {\"answer\": \"I can't delete things from here -- do it in the app itself.\", \"steps\": []}\n"
        "\"tile everything\" -> {\"answer\": \"Arranging all windows.\", \"steps\": [{\"do\": \"arrange\", \"mode\": \"grid\"}]}\n"
        "\"how tall is Everest\" -> {\"answer\": \"That is a question, not something to do on the desktop -- ask PosterChan AI.\", \"steps\": []}\n"
        "A \"post\" is published to Nostr after the user checks it; a \"note\" is private. Only plan steps for things to DO on this desktop.")
    lines = ["Apps (id: name): " + "; ".join(f"{a['view']}: {a['label']}" for a in apps)]
    if windows:
        lines.append("Open windows:\n" + "\n".join(
            f"{i + 1}. {w['title']} (app {w['view'] or '?'}, workspace {w['ws']}{', in front' if w['focused'] else ''})"
            for i, w in enumerate(windows)))
    else:
        lines.append("No windows are open.")
    lines.append(f"Workspaces: {workspaces}.")
    if today and svc._DATE.match(today):
        lines.append(f"Today is {today}.")
    lines.append("Request: " + instruction)
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


def _int(v, lo, hi):
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


_WORD = re.compile(r"[a-z0-9]+")


def _words(v) -> set:
    return set(_WORD.findall(str(v or "").lower()))


def _app_named(s: dict, apps: list, extra=()) -> str:
    """The app a step means: its own field when that is a real id, else the one app whose id or name appears in
    what the model wrote (label/text/app/view). Small models put "Notes" in the label and leave `view` out.
    Never a guess: no match, or two different matches, is None."""
    ids = {a["view"]: a["label"] for a in apps}
    for k in ("view", "app"):
        v = str(s.get(k) or "").strip()
        if v in ids or v in extra:
            return v
    said = set()
    for k in ("view", "app", "label", "text", "name", "target"):
        said |= _words(s.get(k))
    hits = {v for v, label in ids.items() if (_words(v) & said) or (_words(label) and _words(label) <= said)}
    hits |= {e for e in extra if e in said}
    return hits.pop() if len(hits) == 1 else None


def _win_named(s: dict, windows: list, opened: list) -> int | None:
    """A window by number, or by the title/app the model named. `opened` = views opened by earlier steps."""
    try:
        n = int(s.get("win"))
        if 1 <= n <= len(windows) + len(opened):
            return n
    except (TypeError, ValueError):
        pass
    said = set()
    for k in ("win", "window", "app", "view", "label", "text", "name", "target"):
        said |= _words(s.get(k))
    said -= {"close", "snap", "move", "window", "the", "to", "on", "left", "right", "workspace", "put"}
    rows = [(w["title"], w["view"]) for w in windows] + [(v, v) for v in opened]
    hits = [i + 1 for i, (t, v) in enumerate(rows) if (_words(t) and _words(t) <= said) or (v and v in said)]
    return hits[-1] if hits and len(set(rows[h - 1][1] for h in hits)) == 1 else None


def _level(s: dict):
    """(level 0-100 or None, mute True/False/None) from what the model wrote: 30, "30%", "mute", "unmute"."""
    mute = s.get("mute")
    if isinstance(mute, str):
        mute = {"true": True, "false": False}.get(mute.strip().lower())
    lv = s.get("level", s.get("volume"))
    if isinstance(lv, str):
        t = lv.strip().lower()
        if t in ("mute", "muted", "off", "silent"):
            return None, True
        if t in ("unmute", "unmuted", "on"):
            return None, False
        lv = t.rstrip("%").strip()
    n = _int(lv, 0, 100) if lv is not None and lv != "" else None
    return n, mute if isinstance(mute, bool) else None


def parse_steps(text: str, apps: list, windows: list, workspaces: int, instruction: str = "") -> dict:
    """Keep only steps the client can run as stated; everything else is dropped, never guessed at."""
    obj = svc._json_object(text) or {}
    views = {a["view"] for a in apps}
    sound_apps = views | {"music", "other"} | {w["view"] for w in windows if w["view"]}
    answer = svc._clean(obj.get("answer"), 400) if isinstance(obj.get("answer"), str) else ""
    steps, opened = [], []
    asked = _words(instruction)
    for s in obj.get("steps") or []:
        if not isinstance(s, dict) or len(steps) >= STEP_MAX:
            continue
        kind = str(s.get("do") or "").strip().lower()
        if kind not in KINDS:
            continue
        st = {"do": kind}
        if kind == "open":
            v = _app_named(s, apps)
            if v is None or v not in views:
                continue
            st["view"] = v
            opened.append(v)
        elif kind in ("snap", "move", "close"):
            win = _win_named(s, windows, opened)
            if win is None:
                continue
            st["win"] = win
            if kind == "snap":
                z = str(s.get("zone") or "").strip()
                if z not in ZONES:
                    continue
                st["zone"] = z
            if kind == "move":
                n = _int(s.get("n", s.get("workspace")), 1, min(WORKSPACES_MAX, workspaces + 1))
                if n is None:
                    continue
                st["n"] = n
        elif kind == "arrange":
            m = str(s.get("mode") or "grid").strip().lower().replace(" ", "-")
            if m not in ARRANGE:
                continue
            st["mode"] = m
        elif kind == "workspace":
            n = _int(s.get("n", s.get("workspace")), 1, min(WORKSPACES_MAX, workspaces + 1))
            if n is None:
                continue
            st["n"] = n
        elif kind == "volume":
            app = _app_named(s, apps, extra=("music", "other")) 
            if app is None or app not in sound_apps:
                continue
            st["app"] = app
            lv, mute = _level(s)
            if mute is not None:
                st["mute"] = mute
            elif lv is not None:
                st["level"] = lv
            else:
                continue
        elif kind == "screenshot":
            m = str(s.get("mode") or "").strip().lower()
            m = {"area": "region", "part": "region", "selection": "region", "full": "screen", "": "screen"}.get(m, m)
            # The person said which; a model defaulting to the whole screen does not overrule "part of the screen".
            if m == "screen" and asked & {"part", "area", "region", "portion", "selection", "snip"}:
                m = "region"
            if m not in SHOTS:
                continue
            st["mode"] = m
        elif kind in ("note", "post", "search"):
            t = str(s.get("text") or s.get("query") or s.get("content") or "").strip()[: 4000 if kind == "note" else 2000 if kind == "post" else 200]
            if not t or re.search(r"\[(?:your|insert|add)\b[^\]]*\]", t, re.I):
                continue
            st["text"] = t
        label = svc._clean(s.get("label"), 40)
        st["label"] = label or _default_label(st, apps, windows)
        steps.append(st)
    if not answer:
        answer = "Here is how to do that." if steps else "I can't do that from here."
    return {"answer": answer, "steps": steps}


def _default_label(st: dict, apps: list, windows: list) -> str:
    k = st["do"]
    name = {a["view"]: a["label"] for a in apps}
    if k == "open":
        return "Open " + name.get(st["view"], st["view"])
    if k == "snap":
        return "Snap to " + ZONES[st["zone"]]
    if k == "workspace":
        return f"Go to workspace {st['n']}"
    if k == "move":
        return f"Move to workspace {st['n']}"
    if k == "volume":
        return ("Mute " if st.get("mute") else "Unmute " if st.get("mute") is False else "Volume ") + name.get(st["app"], st["app"])
    return {"arrange": "Arrange windows", "screenshot": "Take a screenshot", "record": "Record the screen",
            "note": "Save note", "post": "Draft the post", "search": "Search Nostr", "close": "Close window"}.get(k, k)


async def desktop_steps(db, user, instruction: str, apps, windows, workspaces=1, today: str = "") -> dict:
    instruction = re.sub(r"\s+", " ", str(instruction or "")).strip()[:svc.INSTRUCTION_MAX]
    if not instruction:
        raise svc.AssistError(400, "Say what you want done.")
    apps, windows = clean_apps(apps), clean_windows(windows)
    try:
        workspaces = max(1, min(WORKSPACES_MAX, int(workspaces or 1)))
    except (TypeError, ValueError):
        workspaces = 1
    out = await svc._chat(db, user, build_messages(instruction, apps, windows, workspaces, today), 0.1)
    plan = parse_steps(out, apps, windows, workspaces, instruction)
    # Sizes only: what a person asked for is theirs (feedback_no_prompt_logging).
    logger.info("[chat-assist] desktop: %d chars -> %d steps", len(instruction), len(plan["steps"]))
    return plan
