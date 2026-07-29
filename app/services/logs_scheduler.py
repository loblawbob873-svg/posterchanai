"""System health report — agentic edition.

Runs on a cron schedule (default 01:00, 12:00, 18:00) and on demand via the `/logs` command.
Instead of a hardcoded sequence of shell commands + regex parsing, it drives the existing
agentic node tooling (``node_service.run_agent``): for each configured node it hands the model a
fixed health-check goal and lets it run read-only diagnostic commands, then files the model's
report into the admin's "Logs" conversation and (optionally) Telegram.

All command execution, SSH, per-command timeouts, job logging and live streaming are delegated to
``node_service`` — this module just orchestrates and formats. The set of nodes is the same
Agentic Node Management config (Admin → Nodes); ``logs_nodes`` optionally narrows it.
"""
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import SessionLocal
from app.models import User, Conversation, Message
from app.services import node_service, settings_store
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Global scheduler instance
logs_scheduler: Optional[AsyncIOScheduler] = None

# Name of the Logs conversation
LOGS_CHAT_TITLE = "Logs"

# The goal handed to the agent for each node. It pins down BOTH the emoji semantics and an exact
# line template so every node's report is formatted identically (the model otherwise drifts on
# layout and mislabels healthy-empty results as ⚪). The model still discovers specifics (drive
# names, whether RAID/btrfs/swap exist) for itself rather than relying on hardcoded device lists.
# What the AGENT does: gather accurate data and summarise it in plain text. It is deliberately NOT
# asked to format/emoji anything — this model reliably gathers but won't honour a strict layout at
# the finish step, so presentation is handled deterministically afterwards (see _to_status_board).
_HEALTH_GOAL = (
    "You are auditing this host. Use read-only commands only.\n\n"
    "Check each of these six subsystems: (1) disk usage of /, /boot and /raid; (2) SMART health of "
    "every physical drive; (3) RAID/mdstat; (4) failed systemd services; (5) swap; (6) WARN/ERROR "
    "lines in the last 6h of journalctl and dmesg (group repeats, ignore routine noise).\n\n"
    "SMART must be ACCURATE: run 'lsblk -d -o NAME,TRAN' to get the real drive names, then "
    "'sudo -n smartctl -H /dev/<name>' on EACH (retry with '-d sat' for USB drives or '-d nvme' "
    "for NVMe if it asks for a device type). Report the literal 'overall-health ... result' per "
    "drive (PASSED/FAILED) and NEVER say 'no data' when it printed PASSED/FAILED. Use 'sudo -n' for "
    "smartctl/dmesg/journalctl so they never block on a password prompt.\n\n"
    "When done, call the finish tool with a brief plain-text summary that, for EACH of the six "
    "subsystems, states the finding and whether it is healthy, a warning, critical, or not present "
    "on this host."
)

# Fallback for a node that can't run the LLM 'agent' loop (the lightweight standalone agent on
# router.lan etc. has no local model). It covers the SAME six subsystems as _HEALTH_GOAL in one
# read-only shell pass — the agent loop is only how a node gathers, so a shell-only node has no
# reason to report less. Its output is distilled into the identical status board by _to_status_board
# (the CONTROLLER runs that model, so the worker never needs an LLM); previously this ran a couple of
# trivia commands and pasted them verbatim, which is why such a node looked nothing like the others.
# Log/dmesg lines are normalised (timestamps, hex ids and digits blanked) then counted, so thousands
# of repeats of one nginx error collapse to a single row instead of flooding the board model.
_HEALTH_SHELL = r"""
echo '== disk =='; df -hP / /boot /raid 2>/dev/null
echo; echo '== smart =='
s=$(for d in $(lsblk -dn -o NAME 2>/dev/null | grep -Ev '^(loop|ram|zram|sr|dm-)'); do
  o=$(sudo -n smartctl -H /dev/$d 2>&1)
  case "$o" in *"device type"*|*"Unknown USB"*) o=$(sudo -n smartctl -H -d sat /dev/$d 2>&1);; esac
  echo "$d: $(echo "$o" | grep -Ei 'overall-health|SMART Health Status|Unavailable|not found|Permission denied|Operation not permitted' | head -1)"
done)
echo "${s:-no drives reported}"
echo; echo '== raid =='; cat /proc/mdstat 2>/dev/null | head -15
echo; echo '== failed systemd units =='
f=$(systemctl --failed --no-legend 2>/dev/null | head -20); echo "${f:-none failed}"
echo; echo '== swap =='; free -h 2>/dev/null | grep -iE '^ *total|swap'
echo; echo '== journal errors 6h =='
sudo -n journalctl --since -6h -p err --no-pager -q 2>&1 |
  sed -E 's/^[A-Z][a-z]{2} +[0-9]+ [0-9:]+ [^ ]+ //; s/[0-9a-f:]{4,}//g; s/[0-9]+/#/g' |
  cut -c1-110 | sort | uniq -c | sort -rn | head -8
echo "total: $(sudo -n journalctl --since -6h -p err --no-pager -q 2>/dev/null | wc -l) error lines"
echo; echo '== dmesg warn/err =='
sudo -n dmesg -T --level=err,warn 2>&1 | grep -v 'IN=.*OUT=' |
  sed -E 's/^\[[^]]*\] //; s/[0-9]+/#/g' | cut -c1-110 | sort | uniq -c | sort -rn | head -6
""".strip()

# Sample lines behind the "Errors (6h)" row. A bare count ("11 journal + 8 dmesg errors", "10 SMART
# errors, 2 I/O timeouts") says something is wrong but never WHAT, which makes the row unactionable —
# you can't tell a dying disk from a chatty nginx. So the top repeat-groups are fetched
# DETERMINISTICALLY (like _node_uptime, NOT via the model, which paraphrases the specifics away) and
# printed verbatim underneath. Grouping keeps one REAL sample line per group: the counting pass in
# _HEALTH_SHELL blanks digits/hex, which is right for tallying repeats and unreadable as evidence.
# The dmesg leg keeps _HEALTH_SHELL's proven filters (err/warn only, minus firewall IN=/OUT= spam) —
# without --level the keyword-matched result was mostly boot-time chatter like 'ata1: SATA max UDMA'.
# `--since` needs util-linux >= 2.37; the fallback covers the whole ring buffer rather than nothing.
_ERROR_SAMPLE_SHELL = r"""
g() { awk '{ k=$0; gsub(/[0-9a-f]{8,}/,"",k); gsub(/[0-9]+/,"#",k);
             if (!(k in s)) s[k]=$0; c[k]++ }
       END { for (k in c) printf "%d\t%s\n", c[k], s[k] }' | sort -rn | head -"$1" | cut -c1-180; }
echo '== journal =='
sudo -n journalctl --since -6h -p err --no-pager -q 2>/dev/null |
  sed -E 's/^[A-Z][a-z]{2} +[0-9]+ [0-9:]+ [^ ]+ //' | g 5
echo '== dmesg =='
{ sudo -n dmesg -T --level=err,warn --since '6 hours ago' 2>/dev/null ||
  sudo -n dmesg -T --level=err,warn 2>/dev/null; } |
  grep -v 'IN=.*OUT=' | sed -E 's/^\[[^]]*\] //' | g 4
""".strip()

# Cap on how much evidence rides along: enough to identify the fault, not enough to bury the board.
_ERROR_SAMPLE_MAX = 8

# Deterministic status board — Python owns the emojis + layout so they're identical on every node.
# The model only supplies a status word + short detail per subsystem (an easy single-shot task);
# the icons and ordering below are never the model's job.
_BOARD_ICON = {"disk": "💾", "smart": "🔧", "raid": "💿", "services": "⚙️", "swap": "🔄", "errors": "📜"}
_BOARD_LABEL = {"disk": "Disk", "smart": "SMART", "raid": "RAID", "services": "Services",
                "swap": "Swap", "errors": "Errors (6h)"}
_BOARD_ORDER = ["disk", "smart", "raid", "services", "swap", "errors"]
_STATUS_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴", "none": "⚪"}

_BOARD_SYS = (
    "You convert a server health summary into a status board. Output EXACTLY six lines, in this "
    "order, and NOTHING else. Each line is 'subsystem|status|detail':\n"
    "disk|<status>|<detail>\nsmart|<status>|<detail>\nraid|<status>|<detail>\n"
    "services|<status>|<detail>\nswap|<status>|<detail>\nerrors|<status>|<detail>\n"
    "<status> is exactly one of: green yellow red none. "
    "green = healthy / passed / none / zero-used; yellow = warning (e.g. disk 75-90%); "
    "red = critical / failed / degraded / errors present; none = subsystem not present on this host "
    "(e.g. no RAID array, no swap). A clean or empty result is green, never none. "
    "'services' is ONLY failed systemd units — green when none failed. Journal/dmesg/log errors "
    "belong to 'errors', NEVER to 'services'. "
    "<detail> is a terse phrase, e.g. '/ 33%, /raid 63%' or 'sda,sdb,nvme PASSED' or 'none failed'. "
    "For 'errors', NAME the sources rather than only counting them — 'ata3 I/O errors, nginx upstream "
    "timeouts' beats '10 SMART errors, 2 I/O timeouts', which identifies nothing. "
    "Use ONLY figures that literally appear in the input — never compute or invent a percentage."
)


def _render_board(raw: str) -> Optional[str]:
    """Parse the model's 'subsystem|status|detail' lines into the fixed emoji board. Returns None if
    too few rows parsed (caller falls back to the plain summary)."""
    rows: dict[str, tuple[str, str]] = {}
    for line in (raw or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        key = parts[0].lower()
        key = next((k for k in _BOARD_ICON if k in key), None)
        if not key:
            continue
        status = parts[1].lower()
        status = next((s for s in _STATUS_EMOJI if s in status), "green")
        rows[key] = (status, parts[2])
    if len(rows) < 4:  # model didn't produce a usable board
        return None
    lines = []
    for key in _BOARD_ORDER:
        if key in rows:
            status, detail = rows[key]
            lines.append(f"{_BOARD_ICON[key]} {_BOARD_LABEL[key]}: {_STATUS_EMOJI[status]} {detail}")
    return "\n".join(lines)


def _clean_sample(line: str) -> str:
    """Drop the characters that would unbalance Telegram MarkdownV1 in an arbitrary log line. `_` is
    deliberately left alone — it's everywhere in unit and device names, and telegram_service already
    retries a failed parse as plain text, so mangling every name is the worse trade."""
    return re.sub(r"\s{2,}", " ", re.sub(r"[*`]", "", line)).strip()


def _parse_error_samples(raw: str) -> list:
    """'<count>\\t<line>' rows under the '== journal ==' / '== dmesg ==' markers → display lines."""
    out, source = [], ""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("=="):
            source = stripped.strip("= ").strip().lower()
            continue
        m = re.match(r"^\s*(\d+)\t(.+)$", line.rstrip())
        if not m:
            continue
        text = _clean_sample(m.group(2))
        if not text:
            continue
        # journal lines carry their own unit prefix ("kernel:", "nginx:"); dmesg lines don't, so say
        # where they came from — otherwise the two sources are indistinguishable in the list.
        prefix = "dmesg: " if source == "dmesg" else ""
        out.append(f"↳ ×{m.group(1)} {prefix}{text}")
    return out[:_ERROR_SAMPLE_MAX]


async def _error_samples(db, admin, name: str, target: str) -> list:
    """Top journal/dmesg error groups for a node, each with one verbatim sample line.

    Best-effort and independent of the agent: it returns [] on any failure (so the board is never
    blocked by it) and it runs even when the agent leg errored, which is exactly when raw evidence
    is worth the most."""
    try:
        if target.startswith("nostr:"):
            raw = await node_service.run_agent_over_nostr(target[6:], _ERROR_SAMPLE_SHELL, mode="shell") or ""
        else:
            job = await node_service.run_to_completion(db, name, target, _ERROR_SAMPLE_SHELL,
                                                       user_id=admin.id, timeout=45)
            raw = job.output or ""
        return _parse_error_samples(raw)
    except Exception as e:
        logger.warning(f"error-sample fetch failed for {name}: {e}")
        return []


def _with_error_samples(board: str, samples: list) -> str:
    """Attach the sample lines directly under the 'Errors (6h)' row, so the evidence sits with the
    count it explains. Appended at the end when there's no such row (e.g. a raw-output fallback body
    or an agent error), rather than dropped."""
    if not samples:
        return board
    block = "\n".join(samples)
    lines = (board or "").splitlines()
    head = f"{_BOARD_ICON['errors']} {_BOARD_LABEL['errors']}"
    for i, line in enumerate(lines):
        if line.startswith(head):
            lines.insert(i + 1, block)
            return "\n".join(lines)
    return f"{(board or '').rstrip()}\n{block}".strip()


async def _node_uptime(db, admin, name: str, target: str) -> str:
    """Best-effort one-line system uptime for a node's report header (e.g. 'up 3 days, 4 hours').
    Read-only + DETERMINISTIC (run directly, not via the agent) so it's always present and accurate.
    Returns '' on any failure so the header just omits it."""
    try:
        cmd = "uptime -p 2>/dev/null || uptime"
        if target.startswith("nostr:"):
            out = (await node_service.run_agent_over_nostr(target[6:], cmd, mode="shell")).strip().splitlines()
        else:
            job = await node_service.run_to_completion(db, name, target, cmd, user_id=admin.id, timeout=20)
            out = (job.output or "").strip().splitlines()
        line = out[0].strip() if out else ""
        # "⚠️ …" (transport failure) and a bare "exit N" (command produced no output) are status, not an
        # uptime — showing either as the header's ⏱️ line is worse than omitting it.
        if not line or line.startswith("⚠️") or re.fullmatch(r"exit -?\d+", line):
            return ""
        return line[:80]
    except Exception as e:
        logger.warning(f"uptime fetch failed for {name}: {e}")
        return ""


async def _to_status_board(chat_service, summary: str, fallback: Optional[str] = None) -> str:
    """Turn the agent's plain-text summary (or a shell probe's raw output) into the deterministic emoji
    board. On any failure, fall back to `fallback` — or the input itself — so a node is never blank."""
    fb = (fallback if fallback is not None else summary).strip()
    try:
        raw = await chat_service.chat([
            {"role": "system", "content": _BOARD_SYS},
            {"role": "user", "content": summary},
        ])
        return _render_board(raw) or fb
    except Exception as e:
        logger.warning(f"status-board formatting failed: {e}")
        return fb


def get_logs_settings(db=None) -> dict:
    """Read scheduler settings (schedule + node selection) from the DB, with defaults."""
    settings = {"schedule": "1,12,18", "nodes": []}

    schedule_value = settings_store.get("logs_schedule", "")
    if schedule_value:
        settings["schedule"] = schedule_value

    nodes_value = settings_store.get("logs_nodes", "")
    if nodes_value:
        settings["nodes"] = [n.strip() for n in nodes_value.split(",") if n.strip()]

    return settings


# Per-node run state for the report: {"ok": <unix ts of last SUCCESSFUL report>, "attempt": <unix ts of
# last attempt>}. The `_last_runs` suffix deliberately makes this key node-LOCAL (settings_store
# _RUNTIME_SUFFIXES): each node keeps its own schedule position, and hydrating a peer's copy off the
# relay would make one node think another's report was its own.
_RUN_STATE_KEY = "logs_report_last_runs"
_CATCHUP_MAX_AGE = 24 * 3600   # don't resurrect a report older than a day — it'd describe a stale system
_CATCHUP_RETRY_GAP = 1800      # min seconds between attempts, so a crash-restart loop can't hammer it
_CATCHUP_DELAY = 120           # let the relay/LLM finish coming up before a catch-up run


def _run_state() -> dict:
    import json as _json
    try:
        return _json.loads(settings_store.get(_RUN_STATE_KEY, "") or "{}")
    except Exception:
        return {}


def _mark_run(field: str) -> None:
    """Stamp 'attempt' (run started) or 'ok' (report delivered). Best-effort: losing a stamp costs at
    worst one duplicate report, whereas raising here would abort a report that otherwise succeeded."""
    import json as _json
    try:
        st = _run_state()
        st[field] = int(datetime.now().timestamp())
        settings_store.put(_RUN_STATE_KEY, _json.dumps(st))
    except Exception as e:
        logger.debug("could not stamp report run state (%s): %s", field, e)


def _missed_slot(schedule: str) -> Optional[datetime]:
    """The most recent scheduled slot that has already passed, or None if the schedule is unusable.

    Exists because the report takes ~5 minutes to build and a service restart inside that window
    killed it silently, with no retry — an 18:00 report was lost exactly this way, and nothing noticed
    until a human asked where it went. Comparing this against the last SUCCESSFUL run tells us at
    startup whether we owe one."""
    try:
        hours = sorted({int(h) for h in str(schedule).split(",") if h.strip().isdigit()})
    except Exception:
        return None
    if not hours:
        return None
    now = datetime.now()
    today = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in hours]
    passed = [t for t in today if t <= now]
    if passed:
        return passed[-1]
    # Nothing today yet (e.g. 00:30 with slots 1,12,18) → the last slot of YESTERDAY is the one due.
    return today[-1] - timedelta(days=1)


def _owed_report(schedule: str) -> bool:
    """True when a scheduled report was missed and is still worth running now."""
    slot = _missed_slot(schedule)
    if slot is None:
        return False
    slot_ts = int(slot.timestamp())
    now_ts = int(datetime.now().timestamp())
    if now_ts - slot_ts > _CATCHUP_MAX_AGE:
        return False                                  # too old to be a useful picture of the system
    st = _run_state()
    if int(st.get("ok") or 0) >= slot_ts:
        return False                                  # that slot already produced a report
    if now_ts - int(st.get("attempt") or 0) < _CATCHUP_RETRY_GAP:
        return False                                  # just tried; don't hammer on a restart loop
    return True


async def _catchup_run():
    """One-shot catch-up for a scheduled report that a restart interrupted."""
    if not settings_store.get_bool("logs_scheduler_enabled"):
        return
    logger.info("Health report: running CATCH-UP for a missed/interrupted scheduled run")
    await run_logs_for_admin()


def selected_nodes(db) -> dict:
    """Return {name: target} for the nodes to include in the report. Nostr-only: the shared
    `node_service.all_nodes` registry — synthetic ``local`` (this host, direct) + the npub workers
    (`node_exec_node_npubs`) as ``nostr:<pkhex>``; a self-mapped npub collapses to ``local`` so the
    host is never reported twice. The loop dispatches ``nostr:`` targets over the encrypted channel and
    ``local`` directly. ``logs_nodes`` (if set) narrows by name; empty = all."""
    available = node_service.all_nodes(db)
    chosen = get_logs_settings(db)["nodes"]
    if not chosen:
        return available
    return {name: available[name] for name in chosen if name in available}


def get_or_create_logs_chat(db, user_id: int) -> Conversation:
    """Get the Logs chat for a user, creating it if it doesn't exist."""
    logs_chat = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.title == LOGS_CHAT_TITLE,
    ).first()
    if logs_chat:
        return logs_chat

    logs_chat = Conversation(user_id=user_id, title=LOGS_CHAT_TITLE)
    db.add(logs_chat)
    db.commit()
    db.refresh(logs_chat)
    logger.info(f"Created Logs chat for user {user_id}")
    return logs_chat


def _to_telegram_markdown(text: str) -> str:
    """Convert standard markdown to Telegram Markdown v1 format."""
    import re
    # ## Heading → *Heading* (Telegram MarkdownV1 bold). No backslash before the * — MarkdownV1
    # doesn't honour escapes, so a literal "\*" would show up verbatim instead of bolding.
    text = re.sub(r'^#+ (.+)$', r'*\1*', text, flags=re.MULTILINE)
    # **bold** → *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # Remove leading spaces from indented lines (Telegram ignores indentation)
    text = re.sub(r'^  +', '', text, flags=re.MULTILINE)
    return text


async def build_health_report(db, admin: User, notify=None) -> str:
    """Run the agentic health check across the selected nodes and return the formatted report.

    `notify`, when given, is an async callback used to stream live progress to the originating
    channel (the interactive `/logs` command passes one; the scheduler does not)."""
    nodes = selected_nodes(db)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not node_service.is_enabled(db):
        return (f"## 🩺 System Health Report\n🕒 {timestamp}\n\n"
                "⚠️ Agentic Node Management is disabled — enable it in **Admin → Nodes** to run "
                "the agentic health report.")
    if not nodes:
        return (f"## 🩺 System Health Report\n🕒 {timestamp}\n\n"
                "⚠️ No nodes selected. Configure nodes in **Admin → Nodes → Agentic Node "
                "Management**.")

    chat_service = ChatService(db, admin)
    sections = []
    for name, target in nodes.items():
        _nostr = target.startswith("nostr:")
        where = "this host" if target == "local" else (f"🛰️ nostr:{target[6:18]}…" if _nostr else target)
        if notify:
            try:
                await notify(f"🔍 Checking *{name}* ({where})…")
            except Exception:
                pass
        fallback = None
        try:
            if _nostr:
                summary = await node_service.run_agent_over_nostr(target[6:], _HEALTH_GOAL, mode="agent", report=True)
                # A lightweight standalone worker (no local LLM) can't run agent mode — gather the same
                # six subsystems with one read-only shell probe instead. The board is still distilled
                # here on the controller, so such a node reports exactly like a full one; only the raw
                # probe output (in a code block) is used if that distillation fails.
                if summary and "no local LLM" in summary:
                    raw = (await node_service.run_agent_over_nostr(target[6:], _HEALTH_SHELL, mode="shell") or "").strip()
                    summary = raw[:4000]
                    fallback = f"```\n{raw[:2500] or '(no output)'}\n```"
            else:
                summary = await node_service.run_agent(
                    db, admin, name, target, _HEALTH_GOAL, chat_service,
                    notify=notify, report_mode=True,
                )
            # Presentation is deterministic (Python owns emojis/layout) so the agent model's
            # formatting drift never reaches the report.
            body = await _to_status_board(chat_service, summary or "", fallback=fallback)
        except Exception as e:
            logger.error(f"Health check failed for node {name}: {e}")
            body = f"⚠️ agent error: {e}"
        # The counts on the errors row are a summary, not evidence — attach the actual top log lines
        # so the row can be acted on without opening a shell on the node.
        body = _with_error_samples(body, await _error_samples(db, admin, name, target))
        uptime = await _node_uptime(db, admin, name, target)
        header = f"━━━━━━━━━━━━━━\n🖥️ *{name}*  ·  `{where}`"
        if uptime:
            header += f"\n⏱️ {uptime}"
        sections.append(f"{header}\n\n{(body or '').strip()}")

    body = "\n\n".join(sections)
    return f"## 🩺 System Health Report\n🕒 {timestamp}\n\n{body}"


async def run_logs_for_admin(return_text: bool = False, notify=None,
                             deliver_telegram: bool = True) -> Optional[str]:
    """Build the health report and store it in the admin's Logs conversation + Telegram.

    Returns the report text when `return_text` is True (used by the interactive `/logs` command),
    otherwise None (the scheduler / admin trigger ignore the return).

    `deliver_telegram` pushes the report to the admin's Telegram. The scheduler and admin trigger
    want this; the interactive `/logs` command sets it False because its return value is already
    posted back to whatever channel invoked it (so it'd otherwise arrive twice on Telegram)."""
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.id == 1).first()
        if not admin:
            logger.warning("Admin user not found")
            return None

        logger.info("Building agentic system health report...")
        # Stamp the ATTEMPT before the multi-minute build: if a restart (or a crash) kills us partway,
        # the startup catch-up sees an attempt with no matching success and knows a report is owed —
        # while the gap check stops a restart loop from rebuilding it over and over.
        _mark_run("attempt")
        message_text = await build_health_report(db, admin, notify=notify)

        # build_health_report's per-node agent diagnostics can run for minutes, idling THIS transaction
        # past Postgres' idle_in_transaction_session_timeout (60s). The connection is then dead and the
        # very next query (the "Logs" conversation lookup) throws "server closed the connection
        # unexpectedly". Do the SAVE on a guaranteed-fresh session so the write can't hit a dead conn.
        try:
            db.close()
        except Exception:
            pass
        db = SessionLocal()
        admin = db.query(User).filter(User.id == 1).first()
        if not admin:
            logger.warning("Admin user not found after report build")
            return message_text if return_text else None

        # Store in the admin's Logs conversation
        logs_chat = get_or_create_logs_chat(db, admin.id)
        from app.services import chat_history
        await chat_history.append(db, admin, logs_chat.id, "assistant", message_text)   # encrypted event
        logs_chat.updated_at = datetime.utcnow()
        db.commit()
        logger.info("Added health report to Logs chat for admin")
        # The Logs conversation is created directly here (not via the API that normally mirrors its
        # index doc), so mirror it so the relay is consistent for a fresh-node rebuild. The report
        # MESSAGE is mirrored by the Message after_commit hook (this runs in the scheduler's async
        # loop, so the hook fires); the client shows it from PG via the API regardless.
        try:
            from app.services import chat_store
            await chat_store.mirror_conversation(db, admin, logs_chat)
        except Exception as e:
            logger.warning(f"Logs conversation relay mirror failed: {e}")

        # Send to Telegram if the admin has it enabled (suppressed for the interactive command,
        # whose return value is already delivered to the invoking channel).
        if deliver_telegram and admin.telegram_enabled and admin.telegram_chat_id:
            from app.services.telegram_service import telegram_service, configure_from_settings
            try:
                token = settings_store.get("telegram_bot_token", "")
                if token:
                    telegram_service.set_token(token)
                configure_from_settings(db)
                await telegram_service.send_message(
                    admin.telegram_chat_id,
                    _to_telegram_markdown(message_text),
                )
                logger.info(f"Sent health report to Telegram for admin user {admin.id}")
            except Exception as tg_err:
                logger.error(f"Failed to send health report to Telegram: {tg_err}")

        # Delivered (it's in the Logs conversation; a failed Telegram push is caught above and doesn't
        # un-deliver it). Stamping success here is what stops the next startup from filing a duplicate.
        _mark_run("ok")
        return message_text if return_text else None

    except Exception as e:
        logger.error(f"Error in health report: {e}")
        # The error may itself be a dead connection (idle-timeout during the long build) — a rollback
        # on it would raise again and mask the real error, so guard it.
        try:
            db.rollback()
        except Exception:
            pass
        return f"⚠️ Error generating health report: {e}" if return_text else None
    finally:
        try:
            db.close()
        except Exception:
            pass


async def check_and_run_logs():
    """Scheduler entry point: run the report only if the scheduler is enabled."""
    if not settings_store.get_bool("logs_scheduler_enabled"):
        logger.debug("Logs scheduler disabled")
        return

    await run_logs_for_admin()


def start_logs_scheduler():
    """Start the logs scheduler."""
    global logs_scheduler

    if logs_scheduler is not None:
        logger.warning("Logs scheduler already running")
        return

    if not settings_store.get_bool("logs_scheduler_enabled"):
        logger.info("Logs scheduler disabled")
        return
    schedule = get_logs_settings()["schedule"]

    logs_scheduler = AsyncIOScheduler()
    logs_scheduler.add_job(
        check_and_run_logs,
        CronTrigger(hour=schedule, minute="0"),
        id="logs_scheduler",
        name="System Health Report",
        replace_existing=True,
    )
    # Catch-up: a report that a restart interrupted (or that this node was down for) is re-run shortly
    # after startup instead of being silently skipped until the next slot — up to ~11 hours away on the
    # default 1,12,18 schedule. Delayed rather than inline so the relay/LLM are up first, and gated by
    # _owed_report so it can't duplicate a report that already landed or spin on a restart loop.
    if _owed_report(schedule):
        logs_scheduler.add_job(
            _catchup_run,
            "date",
            run_date=datetime.now() + timedelta(seconds=_CATCHUP_DELAY),
            id="logs_scheduler_catchup",
            name="System Health Report (catch-up)",
            replace_existing=True,
        )
        logger.info("Health report: a scheduled run was missed — catch-up queued in %ds", _CATCHUP_DELAY)

    logs_scheduler.start()
    logger.info(f"Logs scheduler started - running at hours: {schedule}")


def stop_logs_scheduler():
    """Stop the logs scheduler."""
    global logs_scheduler

    if logs_scheduler is not None:
        logs_scheduler.shutdown()
        logs_scheduler = None
        logger.info("Logs scheduler stopped")
