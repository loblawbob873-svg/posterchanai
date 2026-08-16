"""Drive the Telegram update handler with synthetic updates for a LINKED user, exercising
the command-dispatch (messages.py) and callback (callbacks.py) paths after the refactor.

Telegram network sends AND CommandService.execute_command are mocked, so this tests the
telegram LAYER (parse -> dispatch -> keyboards/senders/callbacks -> send) WITHOUT triggering
GPU/LLM/network. A failure is only a real problem if it's an Import/Name/AttributeError
(refactor break); other exceptions are logged as 'ran' (the branch executed).

Run:  venv-unified/bin/python scripts/test_telegram_features.py
"""
import os, sys, asyncio, inspect, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Capture ERROR logs — the handler swallows exceptions and logs them, so a feed that "returns"
# can still have failed internally. We surface those as failures.
_errlog = []
class _Cap(logging.Handler):
    def emit(self, r):
        if r.levelno >= logging.ERROR:
            _errlog.append(r.getMessage())
logging.getLogger().addHandler(_Cap())
logging.getLogger().setLevel(logging.INFO)

from app.database import SessionLocal
from app.models import User
import app.services.telegram_service as tsmod

# --- mock telegram_service (record, no network) ---
sent = []
ts = tsmod.telegram_service
for _n in dir(ts):
    _fn = getattr(ts, _n)
    if inspect.iscoroutinefunction(_fn):
        def _mk(n):
            async def stub(*a, **k):
                sent.append(n)
                return {"ok": True, "result": {"message_id": 1}}
            return stub
        setattr(ts, _n, _mk(_n))

# --- mock CommandService.execute_command (avoid GPU/LLM/network) ---
from app.services.command_service import CommandService
async def _fake_exec(self, command, arg, *a, **k):
    return {"type": "text", "content": f"[mock result for {command} {arg}]"}
CommandService.execute_command = _fake_exec

from app.routers.telegram.webhook import _handle_telegram_update

db = SessionLocal()
linked = db.query(User).filter(User.telegram_chat_id.isnot(None)).first()
if not linked:
    print("No linked telegram user in DB."); sys.exit(1)
CHAT = linked.telegram_chat_id
print(f"Testing as {linked.username} (chat_id={CHAT})\n")

REFBREAK = (ImportError, NameError, AttributeError)
_uid = [1000]
def _msg(text):
    _uid[0] += 1
    return {"update_id": _uid[0],
            "message": {"message_id": _uid[0], "chat": {"id": int(CHAT), "type": "private"},
                        "from": {"id": int(CHAT), "is_bot": False, "first_name": "T"}, "text": text}}
def _cb(data):
    _uid[0] += 1
    return {"update_id": _uid[0],
            "callback_query": {"id": f"cq{_uid[0]}", "from": {"id": int(CHAT)},
                               "message": {"message_id": 1, "chat": {"id": int(CHAT)}}, "data": data}}

results = {"ok": [], "ran": [], "BREAK": []}
async def feed(update, label):
    before = len(_errlog)
    try:
        await _handle_telegram_update(update, db)
    except REFBREAK as e:
        results["BREAK"].append((label, f"{type(e).__name__}: {e}"))
        print(f"  !!BREAK {label}: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc(); return
    except Exception as e:
        results["ran"].append(label); print(f"  ran   {label} ({type(e).__name__}: {str(e)[:70]})"); return
    # check for internally-swallowed errors (handler logged ERROR during this feed)
    new_errs = _errlog[before:]
    swallowed = [m for m in new_errs if any(k in m for k in
                 ("UnboundLocal", "NameError", "AttributeError", "ImportError", "not associated"))]
    if swallowed:
        results["BREAK"].append((label, swallowed[0][:120]))
        print(f"  !!BREAK(swallowed) {label}: {swallowed[0][:120]}")
    else:
        results["ok"].append(label); print(f"  ok    {label}")

COMMANDS = [
    "help", "new", "reminders", "remind test in 5 minutes", "pins", "pin latest xrp news",
    "todo", "budget", "bills", "torrents tv", "nyaa naruto", "news", "dailynews",
    "geni a cat", "musicgeni happy song", "videogeni a dog running", "narrate hello",
    "meme top text", "glow", "collage", "translate spanish hola",
    "ytdl https://youtu.be/x", "post hello world", "screenshot https://example.com",
    "search xrp price", "flashcards", "node uptime", "logs",
]
CALLBACKS = [
    "media:fx:themes", "media:fx:sounds", "media:fx:memes", "media:effects",
    "media:chr:cow", "media:fc", "media:translate", "fc:ans:1", "fc:next",
    "fin:refresh", "fin:add", "news:menu", "4c:board:g", "help:effects",
    "lnk:flashcards", "yt:mp3", "ytdlv:send", "t:nav:1", "n:search", "nk:1",
    "rem:list", "pin:run:1", "all:post", "glow:textpost", "prompt:1", "mk:post",
    "plr:post", "mtx:post",
]

async def main():
    print("=== COMMANDS (messages.py dispatch) ===")
    for c in COMMANDS:
        await feed(_msg(c), f"cmd {c.split()[0]!r}")
    print("\n=== CALLBACKS (callbacks.py dispatch) ===")
    for d in CALLBACKS:
        await feed(_cb(d), f"cb {d!r}")

asyncio.run(main())
db.close()
print("\n================ SUMMARY ================")
print(f"total: {sum(len(v) for v in results.values())} | "
      f"ok(clean): {len(results['ok'])} | ran(branch executed): {len(results['ran'])} | "
      f"REFACTOR-BREAKS: {len(results['BREAK'])}")
if results["BREAK"]:
    print("\n!! BREAKS:")
    for l, e in results["BREAK"]: print(f"   {l}: {e}")
sys.exit(1 if results["BREAK"] else 0)
