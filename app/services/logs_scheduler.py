"""System health report — agentic edition.

Runs on a cron schedule (default 01:00, 12:00, 18:00) and on demand via the `/logs` command.
Instead of a hardcoded sequence of shell commands + regex parsing, it drives the existing
agentic node tooling (``node_service.run_agent``): for each configured node it hands the model a
fixed health-check goal and lets it run read-only diagnostic commands, then files the model's
report into the admin's "Logs" conversation and (optionally) Telegram.

All command execution, SSH, per-command timeouts, job logging and live streaming are delegated to
``node_service`` — this module just orchestrates and formats. The set of nodes is the same
Remote Node Management config (Admin → Services); ``logs_nodes`` optionally narrows it.
"""
import logging
from datetime import datetime
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
    "<detail> is a terse phrase, e.g. '/ 33%, /raid 63%' or 'sda,sdb,nvme PASSED' or 'none failed'."
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


async def _to_status_board(chat_service, summary: str) -> str:
    """Turn the agent's plain-text summary into the deterministic emoji board. On any failure, fall
    back to the raw summary so a node is never blank."""
    try:
        raw = await chat_service.chat([
            {"role": "system", "content": _BOARD_SYS},
            {"role": "user", "content": summary},
        ])
        board = _render_board(raw)
        return board or summary.strip()
    except Exception as e:
        logger.warning(f"status-board formatting failed: {e}")
        return summary.strip()


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


def selected_nodes(db) -> dict:
    """Return {name: target} for the nodes to include in the report — just the Remote Node
    Management nodes (Admin → Services). No synthetic ``local``: this host is already one of those
    entries (its own LB IP), so adding ``local`` reported it twice. A node that points back at THIS
    host runs locally (no SSH-to-self). ``logs_nodes`` (if set) narrows by name; empty = all."""
    available = {
        name: ("local" if node_service.is_local_target(target) else target)
        for name, target in node_service.get_nodes(db).items()
    }
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
                "⚠️ Remote Node Management is disabled — enable it in **Admin → Services** to run "
                "the agentic health report.")
    if not nodes:
        return (f"## 🩺 System Health Report\n🕒 {timestamp}\n\n"
                "⚠️ No nodes selected. Configure nodes in **Admin → Services → Remote Node "
                "Management**.")

    chat_service = ChatService(db, admin)
    sections = []
    for name, target in nodes.items():
        where = "this host" if target == "local" else target
        if notify:
            try:
                await notify(f"🔍 Checking *{name}* ({where})…")
            except Exception:
                pass
        try:
            summary = await node_service.run_agent(
                db, admin, name, target, _HEALTH_GOAL, chat_service,
                notify=notify, report_mode=True,
            )
            # Presentation is deterministic (Python owns emojis/layout) so the agent model's
            # formatting drift never reaches the report.
            body = await _to_status_board(chat_service, summary or "")
        except Exception as e:
            logger.error(f"Health check failed for node {name}: {e}")
            body = f"⚠️ agent error: {e}"
        sections.append(f"━━━━━━━━━━━━━━\n🖥️ *{name}*  ·  `{where}`\n\n{(body or '').strip()}")

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
        db.add(Message(conversation_id=logs_chat.id, role="assistant", content=message_text))
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
    logs_scheduler.start()
    logger.info(f"Logs scheduler started - running at hours: {schedule}")


def stop_logs_scheduler():
    """Stop the logs scheduler."""
    global logs_scheduler

    if logs_scheduler is not None:
        logs_scheduler.shutdown()
        logs_scheduler = None
        logger.info("Logs scheduler stopped")
