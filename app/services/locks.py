"""Shared locks for coordinating access to resources."""
import asyncio

# Global lock to ensure only one image is generated at a time
# Used by both image_api.py and command_service.py
image_generation_lock = asyncio.Lock()

# Shared GPU lock to ensure only one type (LLM or Image) runs at a time per node
# This prevents GPU RAM from being maxed out by running both simultaneously
# Used by both LLM services (ipex, llama, ollama) and image generation
gpu_resource_lock = asyncio.Lock()
