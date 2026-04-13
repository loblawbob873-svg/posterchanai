"""
Logs Scheduler - System log collection and AI analysis.

Runs at noon (12:00), 18:00, and 01:00 to:
1. Collect system logs (journalctl, dmesg, smartctl, etc.)
2. Generate AI summary
3. Store in a "Logs" conversation for admin
"""
import logging
import socket
import subprocess
from datetime import datetime
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import User, Conversation, Message, Setting
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Global scheduler instance
logs_scheduler: Optional[AsyncIOScheduler] = None

# Name of the Logs conversation
LOGS_CHAT_TITLE = "Logs"

# Default drives to check SMART status for
DEFAULT_DRIVES = ["sda", "sdb", "nvme0n1"]

# Default patterns to exclude from syslog (noise filtering)
DEFAULT_EXCLUDE_PATTERNS = (
    "HTTP Error when pushing to|aibot|misskey|python|python-firewall|searx|"
    "Ran out of input|vaultwarden::api::icons|nats|webui.sh|ssl:default|reqwest|"
    "Unable to download icon|Invalid claim|Token has expired|is not a global IP!|"
    "kf.bluezqt|SDL|ToolTip|gameoverlayrenderer|i915|steam|kdecoectd|"
    "sr0|proxy|traceid|Vulkan|xkbcommon|kwin_wayland_wrapper|PipeWire|shpchp|"
    "logwatcher-ai|usb-backup|machine-snapshot|handshake|certbot|clam|cuda|llama|"
    "open-webui|synapse|synctl|fail2ban|php|nextcloud|relay|bundle|mastodon|"
    "deno|node|amdgpu|drm|setup_data|dcc_collect|sasl|npm|emaint|bash|killed|"
    "tinyproxy|pm2|woff2|dnsmasq|su|veth|ntp|containerd|airflow|wpa|redis|"
    "NetworkManager|dhcp4|Temperature_Celsius|minio|dcc_readx|dcc_r_token_int|"
    "Current Time|#######################################|tor|sleeping|bitcoin|"
    "mix|rsync|litd|pleroma|gitea|strfry|jellyfin|postgres|lnd|cron|blossom|"
    "journal|poetry|postfix|ssh|signal|target|slice|dbus|ollama|docker|systemd|"
    "dcc_job_summary|pam|Timeout when pushing"
)


def get_logs_settings(db: Session = None) -> dict:
    """Get logs settings from database or use defaults."""
    settings = {
        'drives': DEFAULT_DRIVES,
        'exclude_patterns': DEFAULT_EXCLUDE_PATTERNS,
        'schedule': '1,12,18',
        'hosts': []
    }

    if db is None:
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    try:
        # Get drives setting
        drives_setting = db.query(Setting).filter(Setting.key == "logs_drives").first()
        if drives_setting and drives_setting.value:
            settings['drives'] = [d.strip() for d in drives_setting.value.split(',') if d.strip()]

        # Get exclude patterns setting
        patterns_setting = db.query(Setting).filter(Setting.key == "logs_exclude_patterns").first()
        if patterns_setting and patterns_setting.value:
            settings['exclude_patterns'] = patterns_setting.value

        # Get schedule setting
        schedule_setting = db.query(Setting).filter(Setting.key == "logs_schedule").first()
        if schedule_setting and schedule_setting.value:
            settings['schedule'] = schedule_setting.value

        # Get remote hosts setting
        hosts_setting = db.query(Setting).filter(Setting.key == "logs_hosts").first()
        if hosts_setting and hosts_setting.value:
            settings['hosts'] = [h.strip() for h in hosts_setting.value.split(',') if h.strip()]
    finally:
        if close_db:
            db.close()

    return settings


def run_command(cmd: str, sudo: bool = False) -> str:
    """Run a shell command and return output."""
    try:
        if sudo:
            cmd = f"sudo {cmd}"
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        logger.debug(f"Command failed: {cmd} - {e}")
        return ""


def run_ssh_command(host: str, cmd: str) -> str:
    """Run a command on a remote host via SSH."""
    try:
        ssh_cmd = f"ssh -o ConnectTimeout=10 -o BatchMode=yes {host} '{cmd}'"
        result = subprocess.run(
            ssh_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning(f"SSH timeout for {host}")
        return ""
    except Exception as e:
        logger.warning(f"SSH failed for {host}: {e}")
        return ""


def collect_remote_logs(host: str, settings: dict) -> str:
    """Collect logs from a remote host via SSH."""
    exclude_patterns = settings['exclude_patterns']
    drives = settings['drives']

    date_str = datetime.now().strftime("%Y-%m-%d")
    dmesg_date = datetime.now().strftime("%b %d")

    log_parts = []
    log_parts.append(f"[Server Name: {host} - System Report {date_str}]")

    # Collect syslog
    syslog = run_ssh_command(
        host,
        f"sudo journalctl -S '6 hours ago' 2>/dev/null | grep -Ei 'warn|error' | grep -Evi '{exclude_patterns}' | head -100"
    )
    if syslog:
        log_parts.append(f"[SysLog] {syslog[:2000]}")

    # Collect dmesg
    dmesg = run_ssh_command(
        host,
        f"sudo dmesg -T 2>/dev/null | grep -Ei 'warn|error' | grep -viE 'vfio-pci|shpchp|sdl|i915|amdgpu|iptables' | grep -i '{dmesg_date}' | head -50"
    )
    if dmesg:
        log_parts.append(f"[DMESG] {dmesg[:1000]}")

    # Disk usage
    root_usage = run_ssh_command(host, "df -h / | awk '{ print $5 }' | tail -1")
    if root_usage:
        log_parts.append(f"[Root Disk Usage] {root_usage}")

    # Failed services
    failed = run_ssh_command(host, "systemctl list-units --state failed --no-pager")
    if failed and "0 loaded" not in failed:
        log_parts.append(f"[Failed Services] {failed[:500]}")

    # SMART data for drives
    smart_data = []
    for drive in drives:
        smart = run_ssh_command(host, f"sudo smartctl -a /dev/{drive} 2>/dev/null | grep -i result | cut -d ':' -f2")
        if smart:
            smart_data.append(f"Drive {drive}: {smart}")
    if smart_data:
        log_parts.append(f"[SMART] {' '.join(smart_data)}")

    return " ".join(log_parts)


def collect_system_logs(db: Session = None) -> str:
    """Collect system logs from various sources."""
    # Get settings from database
    settings = get_logs_settings(db)
    drives = settings['drives']
    exclude_patterns = settings['exclude_patterns']

    hostname = socket.gethostname()
    date_str = datetime.now().strftime("%Y-%m-%d")
    dmesg_date = datetime.now().strftime("%b %d")

    log_parts = []
    log_parts.append(f"[Server Name: {hostname} - System Report {date_str}]")

    # Collect app-specific errors first (run-intel.sh, run.py, posterchanai service)
    app_logs = run_command(
        "journalctl -S '6 hours ago' _COMM=run-intel.sh -p warning 2>/dev/null | tail -50"
    )
    if not app_logs:
        app_logs = run_command(
            "journalctl -S '6 hours ago' -u posterchanai -p warning 2>/dev/null | tail -50"
        )
    if app_logs:
        log_parts.append(f"[App Errors] {app_logs[:2000]}")

    # Collect syslog (warnings and errors from last 6 hours)
    syslog = run_command(
        f"journalctl -S '6 hours ago' | grep -Ei 'warn|error' | grep -Evi '{exclude_patterns}' | tail -100",
        sudo=False
    )
    if syslog:
        log_parts.append(f"[SysLog] {syslog[:3000]}")

    # Collect dmesg
    dmesg = run_command(
        f"dmesg -T | grep -Ei 'warn|error' | grep -viE 'vfio-pci|shpchp|sdl|i915|amdgpu|iptables' | grep -i '{dmesg_date}'",
        sudo=True
    )
    if dmesg:
        log_parts.append(f"[DMESG] {dmesg[:1000]}")

    # Check swap info
    swap = run_command("zramctl | grep SWAP", sudo=True)
    if swap:
        log_parts.append(f"[Swap] {swap}")

    # Check SMART data for drives
    smart_data = []
    for drive in drives:
        smart = run_command(f"smartctl -a /dev/{drive} | grep -i result | cut -d ':' -f2", sudo=True)
        if smart:
            smart_data.append(f"Drive {drive}: {smart}")
    if smart_data:
        log_parts.append(f"[SMART] {' '.join(smart_data)}")

    # Disk usage
    root_usage = run_command("df -h / | awk '{ print $5 }' | tail -1")
    if root_usage:
        log_parts.append(f"[Root Disk Usage] {root_usage}")

    boot_usage = run_command("df -h /boot | awk '{ print $5 }' | tail -1")
    if boot_usage:
        log_parts.append(f"[/boot EFI Disk Usage] {boot_usage}")

    # Failed services
    failed = run_command("systemctl list-units --state failed")
    if failed:
        log_parts.append(f"[Failed Services] {failed}")

    # RAID info (if on NAS)
    if "nas" in hostname.lower():
        raid = run_command("cat /proc/mdstat")
        if raid:
            log_parts.append(f"[Raid Status] {raid[:500]}")

        raid_usage = run_command("df -h /raid | awk '{ print $5 }' | tail -1")
        if raid_usage:
            log_parts.append(f"[Raid Disk Usage] {raid_usage}")

        btrfs = run_command("btrfs scrub status /raid | grep Error")
        if btrfs:
            log_parts.append(f"[BTRFS Scrub Status] {btrfs}")

    return " ".join(log_parts)


def get_or_create_logs_chat(db: Session, user_id: int) -> Conversation:
    """Get the Logs chat for a user, creating it if it doesn't exist."""
    logs_chat = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.title == LOGS_CHAT_TITLE
    ).first()

    if logs_chat:
        return logs_chat

    logs_chat = Conversation(
        user_id=user_id,
        title=LOGS_CHAT_TITLE
    )
    db.add(logs_chat)
    db.commit()
    db.refresh(logs_chat)
    logger.info(f"Created Logs chat for user {user_id}")
    return logs_chat


MAX_LOG_DATA_CHARS = 6000

async def generate_log_summary(db: Session, user: User, log_data: str) -> str:
    """Use AI to summarize the collected logs."""
    hostname = socket.gethostname()

    # Clean up the log data — only strip curly quotes/backticks that can confuse shell
    clean_data = log_data.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')

    # Truncate to avoid inference timeouts when many hosts report large logs
    if len(clean_data) > MAX_LOG_DATA_CHARS:
        clean_data = clean_data[:MAX_LOG_DATA_CHARS] + "\n[...truncated]"

    messages = [
        {
            "role": "system",
            "content": "You are a system administrator assistant. Provide a concise summary of system logs. Focus on any warnings, errors, or issues that need attention. Use emojis to highlight status. Do not provide troubleshooting steps or suggestions."
        },
        {
            "role": "user",
            "content": f"Summarize the following system logs for {hostname}: {clean_data}"
        }
    ]

    try:
        chat_service = ChatService(db, user)
        summary = await chat_service.chat(messages)
        return summary
    except Exception as e:
        logger.error(f"Error generating log summary: {e}")
        return f"Error generating summary: {str(e)}\n\nRaw logs:\n{log_data[:2000]}"


async def run_logs_for_admin():
    """Collect logs and store in the admin's Logs conversation."""
    db = SessionLocal()
    try:
        # Get admin user (user ID 1)
        admin = db.query(User).filter(User.id == 1).first()
        if not admin:
            logger.warning("Admin user not found")
            return

        logger.info("Collecting system logs...")

        # Get settings
        settings = get_logs_settings(db)

        # Collect local logs
        log_data = collect_system_logs(db)

        # Collect logs from remote hosts
        remote_hosts = settings.get('hosts', [])
        logger.info(f"Logs scheduler: remote hosts = {remote_hosts}")
        
        for host in remote_hosts:
            if host:
                logger.info(f"Collecting logs from remote host: {host}")
                remote_log_data = collect_remote_logs(host, settings)
                if remote_log_data:
                    log_data += " " + remote_log_data
                else:
                    logger.warning(f"No log data collected from {host} - SSH may have failed")

        if not log_data:
            logger.info("No log data collected")
            return

        # Generate AI summary
        summary = await generate_log_summary(db, admin, log_data)

        # Get or create Logs chat
        logs_chat = get_or_create_logs_chat(db, admin.id)

        # Format the message
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        hostname = socket.gethostname()
        all_hosts = [hostname] + [h for h in remote_hosts if h]
        hosts_str = ", ".join(all_hosts) if len(all_hosts) > 1 else hostname
        message_text = f"## System Log Report - {hosts_str}\n*{timestamp}*\n\n{summary}"

        # Add message to chat
        log_msg = Message(
            conversation_id=logs_chat.id,
            role="assistant",
            content=message_text
        )
        db.add(log_msg)

        # Update conversation timestamp
        logs_chat.updated_at = datetime.utcnow()
        db.commit()

        logger.info(f"Added log summary to Logs chat for admin")

        # Send to Telegram if admin has Telegram enabled
        if admin.telegram_enabled and admin.telegram_chat_id:
            from app.services.telegram_service import telegram_service
            try:
                telegram_service.set_token(
                    db.query(Setting).filter(Setting.key == "telegram_bot_token").first().value
                )
                await telegram_service.send_message(
                    admin.telegram_chat_id,
                    message_text
                )
                logger.info(f"Sent log summary to Telegram for admin user {admin.id}")
            except Exception as tg_err:
                logger.error(f"Failed to send logs to Telegram: {tg_err}")

    except Exception as e:
        logger.error(f"Error in logs scheduler: {e}")
        db.rollback()
    finally:
        db.close()


async def check_and_run_logs():
    """Check if logs collection is enabled and run."""
    db = SessionLocal()
    try:
        # Check if logs scheduler is enabled
        logs_enabled = db.query(Setting).filter(Setting.key == "logs_scheduler_enabled").first()
        if not logs_enabled or logs_enabled.value.lower() != "true":
            logger.debug("Logs scheduler disabled")
            return

        await run_logs_for_admin()

    except Exception as e:
        logger.error(f"Error in logs scheduler check: {e}")
    finally:
        db.close()


def start_logs_scheduler():
    """Start the logs scheduler."""
    global logs_scheduler

    if logs_scheduler is not None:
        logger.warning("Logs scheduler already running")
        return

    # Check if enabled and get schedule
    db = SessionLocal()
    try:
        enabled_setting = db.query(Setting).filter(Setting.key == "logs_scheduler_enabled").first()
        if not enabled_setting or enabled_setting.value.lower() != "true":
            logger.info("Logs scheduler disabled")
            return

        # Get schedule from settings
        settings = get_logs_settings(db)
        schedule = settings['schedule']
    finally:
        db.close()

    logs_scheduler = AsyncIOScheduler()

    # Run at configured hours
    logs_scheduler.add_job(
        check_and_run_logs,
        CronTrigger(hour=schedule, minute="0"),
        id="logs_scheduler",
        name="System Logs Scheduler",
        replace_existing=True
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
