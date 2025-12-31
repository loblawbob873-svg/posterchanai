"""
Ollama Health Check Service
Periodically pings Ollama and restarts it if unresponsive.
"""
import asyncio
import subprocess
from typing import Optional
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Setting


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
        "restart_command": settings.get("ollama_restart_command", "sudo systemctl restart ollama"),
    }


def restart_ollama(restart_command: str):
    """Restart Ollama using the configured command"""
    print(f"[HEALTH] Restarting Ollama with: {restart_command}")

    # Validate restart command - only allow specific safe commands
    allowed_commands = [
        "sudo /usr/bin/systemctl restart ollama",
        "sudo systemctl restart ollama",
        "systemctl restart ollama",
        "sudo /bin/systemctl restart ollama",
    ]

    if restart_command not in allowed_commands:
        print(f"[HEALTH] ERROR: Restart command '{restart_command}' is not in allowed list")
        print(f"[HEALTH] Allowed commands: {', '.join(allowed_commands)}")
        return False

    try:
        # Use fixed argument list instead of shell parsing for security
        if restart_command.startswith("sudo "):
            result = subprocess.run(
                ["sudo", "/usr/bin/systemctl", "restart", "ollama"],
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
            print("[HEALTH] Ollama restart successful")
            return True
        else:
            print(f"[HEALTH] Ollama restart failed: {result.stderr.decode()}")
            return False

    except subprocess.TimeoutExpired:
        print("[HEALTH] Ollama restart timed out")
        return False
    except Exception as e:
        print(f"[HEALTH] Ollama restart error: {e}")
        return False


async def ping_ollama(ollama_url: str, model: str = "llama3", timeout: float = 30.0) -> bool:
    """Ping Ollama with a simple request"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Use the generate endpoint with a minimal request
            response = await client.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": model,
                    "prompt": "What color is the sky?",
                    "stream": False,
                    "options": {
                        "num_predict": 10  # Very short response
                    }
                }
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("response"):
                    return True

            # Also try the tags endpoint as a fallback
            response = await client.get(f"{ollama_url}/api/tags")
            return response.status_code == 200

    except Exception as e:
        print(f"[HEALTH] Ping failed: {e}")
        return False


async def health_check_loop():
    """Main health check loop"""
    global _consecutive_failures

    print("[HEALTH] Health check loop started")

    while True:
        try:
            # Get fresh settings each iteration
            db = SessionLocal()
            try:
                settings = _get_settings(db)
            finally:
                db.close()

            if not settings["enabled"]:
                print("[HEALTH] Health check disabled, stopping loop")
                break

            # Ping Ollama
            print("[HEALTH] Pinging Ollama...")
            success = await ping_ollama(settings["ollama_url"], settings["ollama_model"])

            if success:
                print("[HEALTH] Ping OK")
                _consecutive_failures = 0
            else:
                _consecutive_failures += 1
                print(f"[HEALTH] Ping FAILED ({_consecutive_failures}/{settings['restart_after_failures']})")

                if _consecutive_failures >= settings["restart_after_failures"]:
                    print("[HEALTH] Too many failures, restarting Ollama...")
                    restart_ollama(settings["restart_command"])
                    _consecutive_failures = 0
                    # Wait a bit for Ollama to restart
                    await asyncio.sleep(10)

            # Wait for next ping
            await asyncio.sleep(settings["ping_interval"])

        except asyncio.CancelledError:
            print("[HEALTH] Health check loop cancelled")
            break
        except Exception as e:
            print(f"[HEALTH] Error in health check loop: {e}")
            await asyncio.sleep(30)  # Wait before retrying


def start_health_check():
    """Start the health check background task"""
    global _health_check_task

    # Check if already running
    if _health_check_task and not _health_check_task.done():
        print("[HEALTH] Health check already running")
        return

    # Check if enabled
    db = SessionLocal()
    try:
        settings = _get_settings(db)
        if not settings["enabled"]:
            print("[HEALTH] Health check is disabled")
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
    print("[HEALTH] Health check started")


def stop_health_check():
    """Stop the health check background task"""
    global _health_check_task

    if _health_check_task and not _health_check_task.done():
        _health_check_task.cancel()
        _health_check_task = None
        print("[HEALTH] Health check stopped")


def is_health_check_running() -> bool:
    """Check if health check is running"""
    return _health_check_task is not None and not _health_check_task.done()
