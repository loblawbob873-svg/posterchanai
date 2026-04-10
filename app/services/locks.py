"""Shared locks for coordinating access to resources."""
import asyncio
import fcntl
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

LOCK_DIR = "/tmp/posterchanai_locks"
os.makedirs(LOCK_DIR, exist_ok=True)

# NOTE: Do NOT clear lock files on startup - this breaks cross-process coordination
# when multiple services (IPEX on 3051, Image on 3052) share the same GPU.
# The fcntl.flock() lock is automatically released when the process exits or crashes.

GPU_LOCK_FILE = os.path.join(LOCK_DIR, "gpu.lock")
CPU_LOCK_FILE = os.path.join(LOCK_DIR, "cpu.lock")  # For CPU mode (both LLM and image)

# In-process lock for async coordination within a single process
_image_generation_lock = asyncio.Lock()
_gpu_lock_base = asyncio.Lock()
_gpu_lock_holder = None


def _try_acquire_file_lock(lock_file: str) -> Optional[int]:
    """Try to acquire file lock without blocking. Returns fd if acquired, None otherwise."""
    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
            return None
    except Exception:
        return None


async def _acquire_file_lock_async(lock_file: str, max_retries: int = 240) -> int:
    """Acquire a file-based lock with async retry logic (non-blocking).
    
    Raises TimeoutError if lock cannot be acquired within max_retries * 0.5 seconds.
    NEVER blocks the event loop - uses non-blocking flock with polling.
    """
    retries = 0
    
    while retries < max_retries:
        fd = _try_acquire_file_lock(lock_file)
        if fd is not None:
            return fd
        
        await asyncio.sleep(0.5)  # Non-blocking sleep
        retries += 1
    
    # DO NOT use blocking flock - it hangs the entire event loop!
    # Instead, raise an error so the request can fail gracefully
    raise TimeoutError(f"Failed to acquire file lock {lock_file} after {max_retries * 0.5:.0f} seconds")


def _release_file_lock(fd: int):
    """Release file-based lock and close descriptor"""
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except Exception:
            pass


class GPUResourceLock:
    """Context manager for GPU/CPU resource lock with file-based locking for cross-process coordination"""
    
    def __init__(self, request_type: str, request_id: str = None, cpu_mode: bool = False):
        """
        Args:
            request_type: "LLM" or "Image"
            request_id: Optional identifier for the request
            cpu_mode: If True, use CPU lock instead of GPU lock
        """
        self.request_type = request_type
        self.request_id = request_id
        self.cpu_mode = cpu_mode
        self.acquired_at = None
        self.wait_start = None
        self._file_lock_fd = None
        self._lock_file = CPU_LOCK_FILE if cpu_mode else GPU_LOCK_FILE
    
    async def __aenter__(self):
        global _gpu_lock_holder
        
        self.wait_start = time.time()
        lock_name = "CPU" if self.cpu_mode else "GPU"
        
        # IMPORTANT: Acquire async lock FIRST (in-process), THEN file lock (cross-process)
        # This prevents holding the file lock while waiting for in-process coordination
        
        # First acquire async lock (within process) - this serializes requests in this process
        if _gpu_lock_holder:
            logger.info(f"[{lock_name}-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} waiting (held by {_gpu_lock_holder})")
        
        await _gpu_lock_base.acquire()
        
        # Now acquire file lock (cross-process) - we're the only one in this process trying
        logger.info(f"[{lock_name}-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} acquired in-process lock, waiting for file lock...")
        try:
            self._file_lock_fd = await _acquire_file_lock_async(self._lock_file)
        except Exception as e:
            # Release async lock if file lock fails
            _gpu_lock_base.release()
            logger.error(f"[{lock_name}-LOCK] Failed to acquire file lock: {e}")
            raise
        
        wait_time = time.time() - self.wait_start
        _gpu_lock_holder = f"{self.request_type}{' ' + self.request_id if self.request_id else ''}"
        self.acquired_at = time.time()
        
        if wait_time > 0.1:
            logger.info(f"[{lock_name}-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} acquired {lock_name} after {wait_time:.2f}s wait")
        else:
            logger.info(f"[{lock_name}-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} acquired {lock_name} (no wait)")
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        global _gpu_lock_holder
        
        lock_name = "CPU" if self.cpu_mode else "GPU"
        hold_time = time.time() - self.acquired_at if self.acquired_at else 0
        
        # Release in reverse order: file lock first, then async lock
        # This allows other processes to acquire the file lock immediately
        
        # Release file lock first (cross-process)
        if self._file_lock_fd:
            _release_file_lock(self._file_lock_fd)
            self._file_lock_fd = None
        
        # Then release async lock (in-process)
        try:
            _gpu_lock_holder = None
            _gpu_lock_base.release()
            logger.info(f"[{lock_name}-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} released {lock_name} (held for {hold_time:.2f}s)")
        except Exception as e:
            logger.error(f"[{lock_name}-LOCK] Error releasing async lock: {e}")


# For backwards compatibility
image_generation_lock = _image_generation_lock


def get_gpu_lock_status():
    """Get current GPU lock status for debugging"""
    return {
        'locked': _gpu_lock_base.locked(),
        'holder': _gpu_lock_holder,
    }


def _log_lock_init():
    """Log lock initialization state (called on module import/startup)"""
    logger.info(f"[GPU-LOCK] Initialized - locked: {_gpu_lock_base.locked()}, holder: {_gpu_lock_holder}")


_log_lock_init()

__all__ = ['image_generation_lock', 'GPUResourceLock', 'get_gpu_lock_status']