"""Shared locks for coordinating access to resources."""
import asyncio

# Global lock to ensure only one image is generated at a time
# Used by both image_api.py and command_service.py
image_generation_lock = asyncio.Lock()
