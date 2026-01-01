"""
LLM Health Check Service
Periodically checks the LLM backend and handles recovery.
Supports native llama-cpp-python, IPEX-LLM, and Ollama backends.
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
logger.propagate = False  # Don't duplicate to root logger
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
        # 120 seconds (2 min) interval for IPEX/Intel Arc GPU health check query
        "ping_interval": int(settings.get("ollama_ping_interval", "120")),
        # Restart after 2 consecutive failures (Intel Arc GPU can be flaky)
        "restart_after_failures": int(settings.get("ollama_restart_after_failures", "2")),
        "restart_command": settings.get("ollama_restart_command", "sudo docker restart ollama-intel-arc"),
        # GPU memory monitoring
        "gpu_memory_check_enabled": settings.get("gpu_memory_check_enabled", "false").lower() == "true",
        "gpu_memory_threshold": int(settings.get("gpu_memory_threshold", "99")),
        "gpu_type": settings.get("gpu_type", "nvidia"),  # "nvidia" or "intel"
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


def reload_ipex_model(db: Session) -> bool:
    """Reload the IPEX-LLM model"""
    logger.info("Reloading IPEX-LLM model...")
    try:
        from app.services.ipex_service import reload_ipex_model as ipex_reload
        ipex_reload(db)
        logger.info("IPEX-LLM model reloaded successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to reload IPEX model: {e}")
        return False


def get_gpu_memory_usage(gpu_type: str = "nvidia") -> Optional[float]:
    """
    Get GPU memory utilization percentage.
    Returns None if unable to get GPU info.
    """
    try:
        if gpu_type == "nvidia":
            # Use nvidia-smi to get memory usage
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                # Parse output like "8192, 16384" (used, total in MiB)
                line = result.stdout.strip().split('\n')[0]  # First GPU
                used, total = map(float, line.split(','))
                percentage = (used / total) * 100
                logger.debug(f"NVIDIA GPU memory: {used:.0f}/{total:.0f} MiB ({percentage:.1f}%)")
                return percentage
            else:
                logger.warning(f"nvidia-smi failed: {result.stderr}")
                return None
        elif gpu_type == "intel":
            # For Intel Arc GPUs, read from i915 debugfs
            # This shows visible_size (total) and visible_avail (free)
            try:
                import re
                # Try debugfs first (most reliable for Intel Arc)
                debugfs_path = "/sys/kernel/debug/dri/0/i915_gem_objects"
                with open(debugfs_path) as f:
                    content = f.read()

                # Parse visible_size and visible_avail (in MiB)
                size_match = re.search(r'visible_size:\s*(\d+)MiB', content)
                avail_match = re.search(r'visible_avail:\s*(\d+)MiB', content)

                if size_match and avail_match:
                    total = int(size_match.group(1))
                    avail = int(avail_match.group(1))
                    used = total - avail
                    percentage = (used / total) * 100
                    logger.debug(f"Intel Arc GPU memory: {used}/{total} MiB ({percentage:.1f}%)")
                    return percentage
            except PermissionError:
                # Try using helper script with sudo
                pass
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug(f"Intel debugfs failed: {e}")

            # Fallback: try helper script with sudo (for non-root processes)
            try:
                import os
                script_path = os.path.join(os.path.dirname(__file__), "../../scripts/gpu_memory.sh")
                script_path = os.path.abspath(script_path)
                if os.path.exists(script_path):
                    result = subprocess.run(
                        ["/usr/bin/sudo", "-n", script_path],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if result.returncode == 0:
                        percentage = float(result.stdout.strip())
                        logger.info(f"Intel Arc GPU VRAM: {percentage:.1f}%")
                        return percentage
            except Exception as e:
                logger.debug(f"GPU helper script failed: {e}")

            # Fallback: try xpu-smi (Intel oneAPI)
            try:
                result = subprocess.run(
                    ["xpu-smi", "stats", "-d", "0", "-j"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    import json
                    data = json.loads(result.stdout)
                    for device in data.get("device_list", [data]):
                        mem_used = device.get("memory_used", 0)
                        mem_total = device.get("memory_physical_size", 0)
                        if mem_total > 0:
                            percentage = (mem_used / mem_total) * 100
                            logger.debug(f"Intel GPU memory (xpu-smi): {mem_used}/{mem_total} ({percentage:.1f}%)")
                            return percentage
            except FileNotFoundError:
                pass
            except Exception:
                pass

            logger.warning("Could not get Intel GPU memory info")
            return None
        else:
            logger.warning(f"Unknown GPU type: {gpu_type}")
            return None
    except subprocess.TimeoutExpired:
        logger.error("GPU memory check timed out")
        return None
    except Exception as e:
        logger.error(f"Error getting GPU memory: {e}")
        return None


def check_gpu_memory_and_reload(db: Session, settings: dict) -> bool:
    """
    Check GPU memory usage and reload model if above threshold.
    Returns True if reload was triggered, False otherwise.
    """
    if not settings["gpu_memory_check_enabled"]:
        return False

    usage = get_gpu_memory_usage(settings["gpu_type"])
    if usage is None:
        return False

    threshold = settings["gpu_memory_threshold"]
    if usage >= threshold:
        logger.warning(f"GPU memory at {usage:.1f}% (threshold: {threshold}%), triggering model reload")

        backend = settings["backend"]
        if backend == "native":
            reload_native_model(db)
        elif backend == "ipex":
            reload_ipex_model(db)
        else:
            # For Ollama, restart the service
            restart_ollama(settings["restart_command"])

        return True

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

        # Model is loaded - that's enough, skip inference test to avoid blocking user requests
        logger.debug("Native model is loaded")
        return True

    except Exception as e:
        logger.error(f"Native ping failed: {e}")
        return False


async def ping_ipex(db: Session) -> bool:
    """Check if IPEX-LLM is loaded and responsive"""
    try:
        from app.services.ipex_service import get_ipex_service

        service = get_ipex_service(db)
        info = service.get_model_info()

        if not info["loaded"]:
            logger.warning("IPEX model not loaded, attempting to load...")
            try:
                service._ensure_model_loaded()
                logger.info("IPEX model loaded successfully")
                return True
            except Exception as e:
                logger.error(f"Failed to load IPEX model: {e}")
                return False

        # Model is loaded - that's enough, skip inference test to avoid blocking user requests
        logger.debug("IPEX model is loaded")
        return True

    except Exception as e:
        logger.error(f"IPEX ping failed: {e}")
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

    # Wait for server to fully start before first health check
    await asyncio.sleep(30)
    logger.info("Starting health checks after initial delay")

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
                elif backend == "ipex":
                    logger.debug("Pinging IPEX-LLM...")
                    success = await ping_ipex(db)
                else:
                    logger.debug(f"Pinging Ollama at {settings['ollama_url']}...")
                    success = await ping_ollama(settings["ollama_url"], settings["ollama_model"])

                if success:
                    _consecutive_failures = 0

                    # Check GPU memory utilization (only if ping succeeded)
                    if settings["gpu_memory_check_enabled"]:
                        usage = get_gpu_memory_usage(settings["gpu_type"])
                        if usage is not None and usage >= settings["gpu_memory_threshold"]:
                            logger.warning(
                                f"GPU VRAM usage at {usage:.1f}% (>= {settings['gpu_memory_threshold']}% threshold) - "
                                f"triggering model reload to free memory"
                            )
                            if backend == "native":
                                reload_native_model(db)
                            elif backend == "ipex":
                                reload_ipex_model(db)
                            else:
                                restart_ollama(settings["restart_command"])
                            # Wait a bit for recovery
                            await asyncio.sleep(10)
                else:
                    _consecutive_failures += 1
                    logger.warning(f"Ping FAILED ({_consecutive_failures}/{settings['restart_after_failures']})")

                    if _consecutive_failures >= settings["restart_after_failures"]:
                        logger.error("Too many failures, attempting recovery...")

                        if backend == "native":
                            # For native, try to reload the model
                            reload_native_model(db)
                        elif backend == "ipex":
                            # For IPEX, try to reload the model
                            reload_ipex_model(db)
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
