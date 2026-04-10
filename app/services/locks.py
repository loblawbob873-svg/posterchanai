"""Shared locks for coordinating access to resources."""
import asyncio
import fcntl
import logging
import os
import time

logger = logging.getLogger(__name__)

LOCK_DIR = "/tmp/posterchanai_locks"
os.makedirs(LOCK_DIR, exist_ok=True)

GPU_LOCK_FILE = os.path.join(LOCK_DIR, "gpu.lock")
CPU_LOCK_FILE = os.path.join(LOCK_DIR, "cpu.lock")  # For CPU mode (both LLM and image)

# In-process lock for async coordination within a single process
_image_generation_lock = asyncio.Lock()
_gpu_lock_base = asyncio.Lock()
_gpu_lock_holder = None


def _acquire_file_lock(lock_file: str, max_retries: int = 120) -> int:
    """Acquire a file-based lock with retry logic to prevent tight spinning"""
    fd = None
    retries = 0
    
    while retries < max_retries:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
                fd = None
        except Exception:
            pass
        
        time.sleep(0.5)
        retries += 1
    
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


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
        
        # First acquire file lock (cross-process)
        lock_name = "CPU" if self.cpu_mode else "GPU"
        logger.info(f"[{lock_name}-LOCK] {self.request_type} request waiting for file lock...")
        self._file_lock_fd = _acquire_file_lock(self._lock_file)
        
        # Then acquire async lock (within process)
        if _gpu_lock_holder:
            logger.info(f"[{lock_name}-LOCK] {self.request_type} request waiting for {lock_name} (currently held by {_gpu_lock_holder})")
        
        try:
            await _gpu_lock_base.acquire()
        except Exception as e:
            _release_file_lock(self._file_lock_fd)
            logger.error(f"[{lock_name}-LOCK] Failed to acquire lock: {e}")
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
        
        try:
            hold_time = time.time() - self.acquired_at if self.acquired_at else 0
            logger.info(f"[{lock_name}-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} released {lock_name} (held for {hold_time:.2f}s)")
            _gpu_lock_holder = None
            _gpu_lock_base.release()
        except Exception as e:
            logger.error(f"[{lock_name}-LOCK] Error releasing async lock: {e}")
        
        # Always release file lock
        if self._file_lock_fd:
            _release_file_lock(self._file_lock_fd)
            self._file_lock_fd = None


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