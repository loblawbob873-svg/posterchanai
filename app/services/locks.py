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

# NOTE: Do NOT clear lock files on startup. The cross-process file lock still guards the
# GPU against any other process (and historically coordinated the old split chat/image
# services). In the unified single-service stack, chat (llama.cpp) and image (a per-gen
# subprocess) serialize on the in-process asyncio lock; the file lock is the belt-and-braces
# cross-process layer. fcntl.flock() releases automatically when the process exits or crashes.

GPU_LOCK_FILE = os.path.join(LOCK_DIR, "gpu.lock")
CPU_LOCK_FILE = os.path.join(LOCK_DIR, "cpu.lock")  # For CPU mode (both LLM and image)

# In-process lock for async coordination within a single process
_image_generation_lock = asyncio.Lock()
_gpu_lock_base = asyncio.Lock()
_gpu_lock_holder = None
# Set by GPUResourceLockSync — a GPU task held from a plain thread, off the event loop.
_sync_lock_holder = None

# Max time a request will WAIT to acquire the GPU lock before failing. Must exceed the longest
# single GPU hold: a multi-second video clip (Wan) can hold the GPU ~5 min, so the old 180s made any
# chat/image/video request queued behind a video gen fail. 630s covers it.
GPU_LOCK_WAIT_TIMEOUT = 630.0


def gpu_busy() -> bool:
    """True if THIS NODE's GPU lock is currently held (an LLM/image/music/video/voice task is
    running). Used by the load-balancing factories to prefer an idle remote node over queueing
    locally. Counts the SYNC holder too, so a model download also pushes new work to an idle
    remote node.

    NODE-wide, not process-wide, and that distinction is the whole point. `_gpu_lock_holder` is a
    module global, so it only ever sees a hold taken by THIS process — but the pollers, the DVM and
    the scheduled health report all run in the separate WORKER process (app/worker.py), and a DVM
    agent job there can hold the GPU for many minutes. Reading only the in-process globals reported
    "idle" for every one of those, so the factories kept routing to a node whose GPU was fully
    occupied and the request discovered the truth by blocking on the flock for up to
    GPU_LOCK_WAIT_TIMEOUT (630s). The flock IS the cross-process layer, so probe it: a
    non-blocking try-acquire that fails means some other process holds the GPU.
    """
    if _gpu_lock_holder is not None or _sync_lock_holder is not None:
        return True
    fd = _try_acquire_file_lock(GPU_LOCK_FILE)
    if fd is None:
        return True        # held by another process (typically the worker)
    os.close(fd)           # closing the fd releases the flock we just took to test it
    return False


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
        
        # Wait long enough to cover the longest GPU hold (a multi-second video clip ~5 min), so
        # requests queued behind a video gen don't spuriously fail.
        try:
            await asyncio.wait_for(_gpu_lock_base.acquire(), timeout=GPU_LOCK_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(f"[{lock_name}-LOCK] Timeout acquiring in-process lock after {GPU_LOCK_WAIT_TIMEOUT:.0f}s (held by {_gpu_lock_holder})")
            raise TimeoutError(f"Failed to acquire {lock_name} lock within {GPU_LOCK_WAIT_TIMEOUT:.0f} seconds, currently held by: {_gpu_lock_holder}")
        
        # Now acquire file lock (cross-process) - we're the only one in this process trying
        logger.info(f"[{lock_name}-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} acquired in-process lock, waiting for file lock...")
        try:
            self._file_lock_fd = await _acquire_file_lock_async(self._lock_file, max_retries=int(GPU_LOCK_WAIT_TIMEOUT / 0.5))
        except BaseException as e:
            # Release async lock if file lock fails (includes CancelledError from task cancellation)
            _gpu_lock_holder = None
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


class GPUResourceLockSync:
    """Blocking, THREAD-safe twin of GPUResourceLock, for GPU work that is NOT on the event loop.

    The model-download worker runs in a plain `threading.Thread`, and there the async lock is
    unusable in both available spellings: `_gpu_lock_base` is an asyncio.Lock bound to the MAIN
    loop, so awaiting it under a fresh `asyncio.run()` attaches futures to the wrong loop, and it
    cannot be awaited at all from sync code. Both download paths therefore ran their VRAM swap and
    model load with NO exclusion — pressing Download mid-song unloaded the music/LLM model out from
    under a running generation and put a second model on the GPU.

    So this takes only the cross-process FILE lock, which is thread- and process-safe. That is
    sufficient: every async GPU task acquires the same file lock (right after the in-process one),
    so while a download holds it, generations wait at that step instead of co-loading. It is also
    deadlock-free — this side never wants the asyncio lock.
    """

    def __init__(self, request_type: str, request_id: str = None, cpu_mode: bool = False,
                 timeout: float = GPU_LOCK_WAIT_TIMEOUT):
        self.request_type = request_type
        self.request_id = request_id
        self.timeout = timeout
        self.acquired_at = None
        self._fd = None
        self._lock_file = CPU_LOCK_FILE if cpu_mode else GPU_LOCK_FILE
        self._label = f"{request_type}{' ' + request_id if request_id else ''}"

    def __enter__(self):
        global _sync_lock_holder
        deadline = time.time() + self.timeout
        while True:
            fd = _try_acquire_file_lock(self._lock_file)
            if fd is not None:
                self._fd = fd
                break
            if time.time() >= deadline:
                raise TimeoutError(
                    f"Failed to acquire GPU lock within {self.timeout:.0f}s for {self._label}"
                )
            time.sleep(0.5)     # a plain thread — blocking here does NOT stall the event loop
        _sync_lock_holder = self._label
        self.acquired_at = time.time()
        logger.info(f"[GPU-LOCK] {self._label} acquired GPU (sync/thread)")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global _sync_lock_holder
        _sync_lock_holder = None
        if self._fd is not None:
            _release_file_lock(self._fd)
            self._fd = None
        held = time.time() - self.acquired_at if self.acquired_at else 0
        logger.info(f"[GPU-LOCK] {self._label} released GPU (sync/thread, held {held:.2f}s)")


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

__all__ = ['image_generation_lock', 'GPUResourceLock', 'GPUResourceLockSync', 'get_gpu_lock_status']