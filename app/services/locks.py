"""Shared locks for coordinating access to resources."""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Global lock to ensure only one image is generated at a time
# Used by both image_api.py and command_service.py
image_generation_lock = asyncio.Lock()

# Shared GPU lock to ensure only one type (LLM or Image) runs at a time per node
# This prevents GPU RAM from being maxed out by running both simultaneously
# Used by both LLM services (ipex, llama, ollama) and image generation
_gpu_lock_base = asyncio.Lock()
_gpu_lock_holder = None  # Track what's currently using the GPU


class GPUResourceLock:
    """Context manager for GPU resource lock with logging"""
    
    def __init__(self, request_type: str, request_id: str = None):
        """
        Args:
            request_type: "LLM" or "Image"
            request_id: Optional identifier for the request
        """
        self.request_type = request_type
        self.request_id = request_id
        self.acquired_at = None
        self.wait_start = None
    
    async def __aenter__(self):
        global _gpu_lock_holder
        self.wait_start = time.time()
        if _gpu_lock_holder:
            logger.info(f"[GPU-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} waiting for GPU (currently held by {_gpu_lock_holder})")
        await _gpu_lock_base.acquire()
        wait_time = time.time() - self.wait_start
        _gpu_lock_holder = f"{self.request_type}{' ' + self.request_id if self.request_id else ''}"
        self.acquired_at = time.time()
        if wait_time > 0.1:  # Only log if waited more than 100ms
            logger.info(f"[GPU-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} acquired GPU after {wait_time:.2f}s wait")
        else:
            logger.debug(f"[GPU-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} acquired GPU (no wait)")
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        global _gpu_lock_holder
        hold_time = time.time() - self.acquired_at if self.acquired_at else 0
        logger.debug(f"[GPU-LOCK] {self.request_type} request{' ' + self.request_id if self.request_id else ''} released GPU (held for {hold_time:.2f}s)")
        _gpu_lock_holder = None
        _gpu_lock_base.release()


# Export the class for use in other modules
__all__ = ['image_generation_lock', 'GPUResourceLock']
