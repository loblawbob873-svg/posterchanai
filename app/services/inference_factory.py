"""
Inference Factory - Returns the appropriate inference service based on settings.
Supports native llama-cpp-python, IPEX-LLM, and Ollama backends.
Integrates with VRAM manager for model swapping on shared GPU.
"""
from sqlalchemy.orm import Session
from typing import Union

from app.models import Setting


def prepare_vram_for_llm(db: Session):
    """Prepare VRAM for LLM inference (swap models if needed)"""
    from app.services.vram_manager import prepare_for_llm
    prepare_for_llm(db)


def get_backend_type(db: Session) -> str:
    """Get the configured backend type from database"""
    setting = db.query(Setting).filter(Setting.key == "llm_backend").first()
    return setting.value if setting else "ollama"  # Default to ollama for backward compatibility


def get_inference_service(db: Session) -> Union["LlamaService", "IPEXService", "OllamaService"]:
    """
    Get the appropriate inference service based on settings.
    Returns LlamaService for native, IPEXService for ipex, OllamaService for ollama.
    """
    backend = get_backend_type(db)

    if backend == "native":
        from app.services.llama_service import get_llama_service
        return get_llama_service(db)
    elif backend == "ipex":
        from app.services.ipex_service import get_ipex_service
        return get_ipex_service(db)
    else:
        from app.services.ollama_service import get_ollama_service
        return get_ollama_service(db)


def reload_inference_model(db: Session):
    """Reload the model for the current backend"""
    backend = get_backend_type(db)

    if backend == "native":
        from app.services.llama_service import reload_llama_model
        reload_llama_model(db)
    elif backend == "ipex":
        from app.services.ipex_service import reload_ipex_model
        reload_ipex_model(db)
    # Ollama doesn't need explicit reload - it handles this itself


def get_inference_status(db: Session) -> dict:
    """Get status information about the inference backend"""
    backend = get_backend_type(db)

    if backend == "native":
        from app.services.llama_service import get_llama_service
        service = get_llama_service(db)
        info = service.get_model_info()
        return {
            "backend": "native",
            "status": "loaded" if info["loaded"] else "not_loaded",
            **info
        }
    elif backend == "ipex":
        from app.services.ipex_service import get_ipex_service
        service = get_ipex_service(db)
        info = service.get_model_info()
        return {
            "backend": "ipex",
            "status": "loaded" if info["loaded"] else "not_loaded",
            **info
        }
    else:
        # Ollama - check if reachable
        settings = {s.key: s.value for s in db.query(Setting).all()}
        ollama_url = settings.get("ollama_url", "http://localhost:11434")
        return {
            "backend": "ollama",
            "status": "configured",
            "ollama_url": ollama_url
        }
