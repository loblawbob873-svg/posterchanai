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
import socket
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import SessionLocal
from app.models import User, Conversation, Message, Setting
from app.services import node_service
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Global scheduler instance
logs_scheduler: Optional[AsyncIOScheduler] = None

# Name of the Logs conversation
LOGS_CHAT_TITLE = "Logs"

# The goal handed to the agent for each node. It steers toward a consistent, scannable layout
# (status emojis, one item per line) while letting the model discover specifics (drive names,
# whether RAID/btrfs exist) for itself rather than relying on hardcoded device lists.
_HEALTH_GOAL = (
    "Produce a concise SYSTEM HEALTH REPORT for this host using read-only commands only. "
    "Investigate and report each of the following, one item per line, each led by a status "
    "emoji (🟢 ok, 🟡 warning, 🔴 critical, ⚪ not present):\n"
    "• 💾 Disk usage for /, /boot and /raid — flag 🟡 above 75%, 🔴 above 90%\n"
    "• 🔧 SMART overall-health of each physical drive (discover them with lsblk; don't assume names)\n"
    "• 💿 RAID/mdstat status and btrfs scrub status, if present\n"
    "• ⚙️ Failed systemd services\n"
    "• 🔄 Swap / zram usage\n"
    "• 📜 Notable WARN/ERROR lines from the last 6h of journalctl and dmesg (group repeats; "
    "ignore routine noise)\n"
    "Use 'sudo -n' for privileged reads (smartctl, dmesg, journalctl) so they never block on a "
    "password prompt; if a command is denied, note it and move on. "
    "Keep it tight — a glanceable status board, not prose. Do NOT include troubleshooting steps. "
    "When finished, call the finish tool with the full formatted report as the summary."
)


def get_logs_settings(db=None) -> dict:
    """Read scheduler settings (schedule + node selection) from the DB, with defaults."""
    settings = {"schedule": "1,12,18", "nodes": []}

    close_db = db is None
    if close_db:
        db = SessionLocal()
    try:
        schedule_setting = db.query(Setting).filter(Setting.key == "logs_schedule").first()
        if schedule_setting and schedule_setting.value:
            settings["schedule"] = schedule_setting.value

        nodes_setting = db.query(Setting).filter(Setting.key == "logs_nodes").first()
        if nodes_setting and nodes_setting.value:
            settings["nodes"] = [n.strip() for n in nodes_setting.value.split(",") if n.strip()]
    finally:
        if close_db:
            db.close()

    return settings


def selected_nodes(db) -> dict:
    """Return {name: target} for the nodes to include in the report.

    Always offers a synthetic ``local`` node (the host running posterchanai) alongside the
    configured Remote Node Management nodes. ``logs_nodes`` (if set) narrows the selection by
    name; empty means "all of them"."""
    available = {"local": "local", **node_service.get_nodes(db)}
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
        except Exception as e:
            logger.error(f"Health check failed for node {name}: {e}")
            summary = f"⚠️ agent error: {e}"
        sections.append(f"━━━━━━━━━━━━━━\n🖥️ *{name}*  ·  `{where}`\n\n{(summary or '').strip()}")

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

        # Store in the admin's Logs conversation
        logs_chat = get_or_create_logs_chat(db, admin.id)
        db.add(Message(conversation_id=logs_chat.id, role="assistant", content=message_text))
        logs_chat.updated_at = datetime.utcnow()
        db.commit()
        logger.info("Added health report to Logs chat for admin")

        # Send to Telegram if the admin has it enabled (suppressed for the interactive command,
        # whose return value is already delivered to the invoking channel).
        if deliver_telegram and admin.telegram_enabled and admin.telegram_chat_id:
            from app.services.telegram_service import telegram_service, configure_from_settings
            try:
                token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
                if token and token.value:
                    telegram_service.set_token(token.value)
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
        db.rollback()
        return f"⚠️ Error generating health report: {e}" if return_text else None
    finally:
        db.close()


async def check_and_run_logs():
    """Scheduler entry point: run the report only if the scheduler is enabled."""
    db = SessionLocal()
    try:
        logs_enabled = db.query(Setting).filter(Setting.key == "logs_scheduler_enabled").first()
        if not logs_enabled or logs_enabled.value.lower() != "true":
            logger.debug("Logs scheduler disabled")
            return
    finally:
        db.close()

    await run_logs_for_admin()


def start_logs_scheduler():
    """Start the logs scheduler."""
    global logs_scheduler

    if logs_scheduler is not None:
        logger.warning("Logs scheduler already running")
        return

    db = SessionLocal()
    try:
        enabled_setting = db.query(Setting).filter(Setting.key == "logs_scheduler_enabled").first()
        if not enabled_setting or enabled_setting.value.lower() != "true":
            logger.info("Logs scheduler disabled")
            return
        schedule = get_logs_settings(db)["schedule"]
    finally:
        db.close()

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
