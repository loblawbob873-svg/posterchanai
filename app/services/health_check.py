"""
LLM Health Check Service
Periodically checks the LLM backend and handles recovery.
Supports both native llama-cpp-python and Ollama backends.
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
        "backend": settings.get("llm_backend", "ollama"),
        "ollama_url": settings.get("ollama_url", "http://localhost:11434"),
        "ollama_model": settings.get("ollama_model", "llama3"),
        "model_path": settings.get("llm_model_path", ""),
        "ping_interval": int(settings.get("ollama_ping_interval", "90")),
        "restart_after_failures": int(settings.get("ollama_restart_after_failures", "5")),
        "restart_command": settings.get("ollama_restart_command", "sudo docker restart ollama-intel-arc"),
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


def reload_native_model(db: Session) -> bool:
    """Reload the native LLM model"""
    logger.info("Reloading native LLM model...")
    try:
        from app.services.llama_service import reload_llama_model
        reload_llama_model(db)
        logger.info("Native LLM model reloaded successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to reload native model: {e}")
        return False


async def ping_native(db: Session) -> bool:
    """Check if native LLM is loaded and responsive"""
    try:
        from app.services.llama_service import get_llama_service

        service = get_llama_service(db)
        info = service.get_model_info()

        if not info["loaded"]:
            logger.warning("Native model not loaded, attempting to load...")
            try:
                service._ensure_model_loaded()
                logger.info("Native model loaded successfully")
                return True
            except Exception as e:
                logger.error(f"Failed to load native model: {e}")
                return False

        # Model is loaded, do a quick inference test
        try:
            # Simple test - just check if we can call the model
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                service.chat_completion(
                    messages=[{"role": "user", "content": "Hi"}],
                    max_tokens=5
                ),
                timeout=30
            )

            if "error" in result:
                logger.error(f"Native model test failed: {result['error']}")
                return False

            logger.info("Native model ping OK")
            return True

        except asyncio.TimeoutError:
            logger.error("Native model test timed out")
            return False
        except Exception as e:
            logger.error(f"Native model test error: {e}")
            return False

    except Exception as e:
        logger.error(f"Native ping failed: {e}")
        return False


async def ping_ollama(ollama_url: str, model: str = "llama3") -> bool:
    """Ping Ollama - just check if it's alive, don't force model loads"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Just check if Ollama is alive with /api/tags (fast, no model load)
            response = await client.get(f"{ollama_url}/api/tags")
            if response.status_code != 200:
                logger.error("Ollama not responding to /api/tags")
                return False

            # Check if model is loaded and refresh keep_alive if so
            ps_response = await client.get(f"{ollama_url}/api/ps")
            if ps_response.status_code == 200:
                ps_data = ps_response.json()
                models = ps_data.get("models", [])
                for m in models:
                    if m.get("name") == model:
                        # Our model is loaded, refresh keep_alive
                        try:
                            await client.post(
                                f"{ollama_url}/api/generate",
                                json={
                                    "model": model,
                                    "prompt": "",
                                    "keep_alive": -1
                                },
                                timeout=10
                            )
                        except Exception:
                            pass  # Keep-alive refresh is best-effort
                        break

            # Ollama is alive, that's what matters
            logger.info("Ollama ping OK")
            return True

    except Exception as e:
        logger.error(f"Ollama ping failed: {e}")
        return False


async def health_check_loop():
    """Main health check loop - supports both native and Ollama backends"""
    global _consecutive_failures

    logger.info("Health check loop started")

    while True:
        try:
            # Get fresh settings each iteration
            db = SessionLocal()
            try:
                settings = _get_settings(db)

                if not settings["enabled"]:
                    logger.info("Health check disabled, stopping loop")
                    break

                backend = settings["backend"]

                # Ping based on backend type
                if backend == "native":
                    logger.debug("Pinging native LLM...")
                    success = await ping_native(db)
                else:
                    logger.debug(f"Pinging Ollama at {settings['ollama_url']}...")
                    success = await ping_ollama(settings["ollama_url"], settings["ollama_model"])

                if success:
                    _consecutive_failures = 0
                else:
                    _consecutive_failures += 1
                    logger.warning(f"Ping FAILED ({_consecutive_failures}/{settings['restart_after_failures']})")

                    if _consecutive_failures >= settings["restart_after_failures"]:
                        logger.error("Too many failures, attempting recovery...")

                        if backend == "native":
                            # For native, try to reload the model
                            reload_native_model(db)
                        else:
                            # For Ollama, restart the service
                            restart_ollama(settings["restart_command"])

                        _consecutive_failures = 0
                        # Wait a bit for recovery
                        await asyncio.sleep(10)

            finally:
                db.close()

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
