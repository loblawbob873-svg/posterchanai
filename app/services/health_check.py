"""
Ollama Health Check Service
Periodically pings Ollama and restarts it if unresponsive.
"""
import asyncio
import logging
import subprocess
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Setting

# Configure logging
logger = logging.getLogger("health_check")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [HEALTH] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)


# Global state for the health check
_health_check_task: Optional[asyncio.Task] = None
_consecutive_failures: int = 0


def _get_settings(db: Session) -> dict:
    """Get health check settings from database"""
    settings = {s.key: s.value for s in db.query(Setting).all()}
    return {
        "enabled": settings.get("ollama_ping_enabled", "false").lower() == "true",
        "ollama_url": settings.get("ollama_url", "http://localhost:11434"),
        "ollama_model": settings.get("ollama_model", "llama3"),
        "ping_interval": int(settings.get("ollama_ping_interval", "90")),
        "restart_after_failures": int(settings.get("ollama_restart_after_failures", "5")),
        "restart_command": settings.get("ollama_restart_command", "sudo docker restart ollama-intel-arc"),
        "num_ctx": int(settings.get("ollama_num_ctx", "40960")),
    }


def restart_ollama(restart_command: str):
    """Restart Ollama using the configured command"""
    logger.warning(f"Restarting Ollama with: {restart_command}")

    # Validate restart command - only allow specific safe commands
    allowed_commands = [
        "sudo /usr/bin/systemctl restart ollama",
        "sudo systemctl restart ollama",
        "systemctl restart ollama",
        "sudo /bin/systemctl restart ollama",
        "sudo docker restart ollama-intel-arc",
        "docker restart ollama-intel-arc",
    ]

    if restart_command not in allowed_commands:
        logger.error(f"Restart command '{restart_command}' is not in allowed list")
        logger.error(f"Allowed commands: {', '.join(allowed_commands)}")
        return False

    try:
        # Use fixed argument list instead of shell parsing for security
        if "docker restart" in restart_command:
            if restart_command.startswith("sudo "):
                result = subprocess.run(
                    ["/usr/bin/sudo", "/usr/bin/docker", "restart", "ollama-intel-arc"],
                    check=False,
                    capture_output=True,
                    timeout=60
                )
            else:
                result = subprocess.run(
                    ["/usr/bin/docker", "restart", "ollama-intel-arc"],
                    check=False,
                    capture_output=True,
                    timeout=60
                )
        elif restart_command.startswith("sudo "):
            result = subprocess.run(
                ["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "ollama"],
                check=False,
                capture_output=True,
                timeout=30
            )
        else:
            result = subprocess.run(
                ["/usr/bin/systemctl", "restart", "ollama"],
                check=False,
                capture_output=True,
                timeout=30
            )

        if result.returncode == 0:
            logger.info("Ollama restart successful")
            return True
        else:
            logger.error(f"Ollama restart failed: {result.stderr.decode()}")
            return False

    except subprocess.TimeoutExpired:
        logger.error("Ollama restart timed out")
        return False
    except Exception as e:
        logger.error(f"Ollama restart error: {e}")
        return False


async def ping_ollama(ollama_url: str, model: str = "llama3", timeout: float = 120.0, num_ctx: int = 40960) -> bool:
    """Ping Ollama - check if alive and model is loaded"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # First just check if Ollama is alive with /api/tags (fast, no model load)
            response = await client.get(f"{ollama_url}/api/tags")
            if response.status_code != 200:
                logger.error("Ollama not responding to /api/tags")
                return False

            # Check if any model is currently loaded
            ps_response = await client.get(f"{ollama_url}/api/ps")
            if ps_response.status_code == 200:
                ps_data = ps_response.json()
                models = ps_data.get("models", [])
                if models:
                    # Model is loaded, just refresh keep_alive without changing context
                    for m in models:
                        if m.get("name") == model:
                            # Our model is loaded, send a minimal keep_alive refresh
                            await client.post(
                                f"{ollama_url}/api/generate",
                                json={
                                    "model": model,
                                    "prompt": "",
                                    "keep_alive": -1
                                },
                                timeout=10
                            )
                            return True
                    # Different model loaded, that's ok
                    return True

            # No model loaded, load it with correct context (will take time)
            async with httpx.AsyncClient(timeout=timeout) as long_client:
                response = await long_client.post(
                    f"{ollama_url}/api/generate",
                    json={
                        "model": model,
                        "prompt": "hi",
                        "stream": False,
                        "keep_alive": -1,
                        "options": {
                            "num_predict": 1,
                            "num_ctx": num_ctx
                        }
                    }
                )
                return response.status_code == 200

    except Exception as e:
        logger.error(f"Ping failed: {e}")
        return False


async def health_check_loop():
    """Main health check loop"""
    global _consecutive_failures

    logger.info("Health check loop started")

    while True:
        try:
            # Get fresh settings each iteration
            db = SessionLocal()
            try:
                settings = _get_settings(db)
            finally:
                db.close()

            if not settings["enabled"]:
                logger.info("Health check disabled, stopping loop")
                break

            # Ping Ollama
            logger.debug(f"Pinging Ollama at {settings['ollama_url']}...")
            success = await ping_ollama(settings["ollama_url"], settings["ollama_model"], num_ctx=settings["num_ctx"])

            if success:
                logger.info("Ping OK")
                _consecutive_failures = 0
            else:
                _consecutive_failures += 1
                logger.warning(f"Ping FAILED ({_consecutive_failures}/{settings['restart_after_failures']})")

                if _consecutive_failures >= settings["restart_after_failures"]:
                    logger.error("Too many failures, restarting Ollama...")
                    restart_ollama(settings["restart_command"])
                    _consecutive_failures = 0
                    # Wait a bit for Ollama to restart
                    await asyncio.sleep(10)

            # Wait for next ping
            await asyncio.sleep(settings["ping_interval"])

        except asyncio.CancelledError:
            logger.info("Health check loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in health check loop: {e}")
            await asyncio.sleep(30)  # Wait before retrying


def start_health_check():
    """Start the health check background task"""
    global _health_check_task

    # Check if already running
    if _health_check_task and not _health_check_task.done():
        logger.info("Health check already running")
        return

    # Check if enabled
    db = SessionLocal()
    try:
        settings = _get_settings(db)
        if not settings["enabled"]:
            logger.info("Health check is disabled")
            return
    finally:
        db.close()

    # Start the task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop, create one
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _health_check_task = loop.create_task(health_check_loop())
    logger.info("Health check started")


def stop_health_check():
    """Stop the health check background task"""
    global _health_check_task

    if _health_check_task and not _health_check_task.done():
        _health_check_task.cancel()
        _health_check_task = None
        logger.info("Health check stopped")


def is_health_check_running() -> bool:
    """Check if health check is running"""
    return _health_check_task is not None and not _health_check_task.done()
