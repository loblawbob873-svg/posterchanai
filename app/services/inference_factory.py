"""
Inference Factory - the local LLM is always the native llama-cpp-python backend
(SYCL on Intel Arc, CUDA on NVIDIA, HIP on AMD, CPU otherwise). Ollama and IPEX-LLM
have been removed. Integrates with the VRAM manager for model swapping on a shared GPU.
"""
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:   # annotation-only; importing at runtime would be a cycle
    from app.services.llama_service import LlamaService


def prepare_vram_for_llm(db: Session):
    """Prepare VRAM for LLM inference (swap models if needed)"""
    from app.services.vram_manager import prepare_for_llm
    prepare_for_llm(db)


def get_backend_type(db: Session) -> str:
    """The local LLM backend is always native llama.cpp."""
    return "native"


def get_inference_service(db: Session) -> "LlamaService":
    """Return the native llama.cpp inference service."""
    from app.services.llama_service import get_llama_service
    return get_llama_service(db)


def reload_inference_model(db: Session):
    """Reload the native model."""
    from app.services.llama_service import reload_llama_model
    reload_llama_model(db)


def get_inference_status(db: Session) -> dict:
    """Get status information about the inference backend."""
    from app.services.llama_service import get_llama_service
    service = get_llama_service(db)
    info = service.get_model_info()
    return {
        "backend": "native",
        "status": "loaded" if info["loaded"] else "not_loaded",
        **info
    }
