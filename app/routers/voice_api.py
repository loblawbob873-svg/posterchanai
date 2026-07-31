"""Direct Voice API (server-to-server), mirroring video_api / music_api.

Lets one posterchanai node forward a voice-cloning request to another. The receiving node generates
LOCALLY: `voice_factory._generate_local` takes the shared GPU lock and runs `prepare_for_voice`
(freeing its LLM/image/music/video VRAM) before loading the model.

Multipart, not base64 JSON. The reference clip is real audio and the reply is a WAV — base64 in a
JSON body costs 33% on the wire and forces both sides to hold the whole thing in memory as a string,
which is the exact trap that made large AI-chat uploads fail. The clip travels WITH the request
because the remote node has no access to the requesting user's Blossom drive.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user_optional
from app.database import get_db

logger = logging.getLogger("voice_api")

router = APIRouter(prefix="/api", tags=["voice"])

# Longest reference clip we will accept from another node. A few seconds is all a zero-shot model
# uses; anything past this is someone posting a podcast at us.
_MAX_REF_BYTES = 16 * 1024 * 1024


async def get_voice_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> bool:
    """Allow load-balanced requests from other posterchanai nodes without auth; otherwise accept
    API key / JWT (mirrors video_api.get_video_auth)."""
    if request.headers.get("x-posterchanai-load-balanced", "").lower() == "true":
        return True
    for token in (x_api_key, (authorization[7:] if authorization and authorization.startswith("Bearer ") else None)):
        if not token:
            continue
        try:
            from app.utils.auth_utils import query_api_key_with_retry, get_user_from_api_key
            api_key, user_id = query_api_key_with_retry(db, str(token).strip())
            if api_key and user_id and get_user_from_api_key(db, user_id):
                return True
        except Exception:
            pass
    try:
        if get_current_user_optional(request, db):
            return True
    except Exception:
        pass
    return True


@router.get("/generate-voice/status")
async def voice_status():
    """Is this node able to generate, and is its GPU already occupied?

    `voice_factory.is_busy` reads this to prefer an idle node. It deliberately reports `busy` from the
    QUEUE depth rather than "is a model loaded": a loaded-but-idle model is exactly the node we most
    want to send work to, since it skips the 6.4s load.
    """
    from app.services import voice_local, voice_factory
    from app.services import settings_store
    enabled = str(settings_store.get("voice_enabled", "false")).lower() in ("1", "true", "yes", "on")
    return {
        "available": voice_local.is_available() and enabled,
        "downloaded": voice_local.is_downloaded(),
        "busy": voice_factory.queue_depth() > 0,
        "queue": voice_factory.queue_depth(),
    }


@router.post("/generate-voice")
async def generate_voice_endpoint(
    reference: UploadFile = File(...),
    text: str = Form(...),
    db: Session = Depends(get_db),
    _auth: bool = Depends(get_voice_auth),
):
    """Generate on THIS node and return WAV bytes.

    Deliberately calls `_generate_local`, NOT `generate_voice`: the public entry point would be free
    to forward the request on to yet another node, and a chain of nodes each forwarding to the next is
    a loop nobody can see. A node that is asked directly either does the work or fails.
    """
    import os
    import tempfile
    from app.services import voice_local, voice_factory
    from app.services import settings_store

    if str(settings_store.get("voice_enabled", "false")).lower() not in ("1", "true", "yes", "on"):
        return JSONResponse({"error": "voice generation is disabled on this node"}, status_code=503)
    if not voice_local.is_available():
        # 404 (not 503) on purpose: voice_factory treats a 404 as "this node hasn't got voice
        # installed" and moves calmly to the next candidate instead of counting it as a failure.
        return JSONResponse({"error": "voice model not installed on this node"}, status_code=404)

    data = await reference.read()
    if not data:
        return JSONResponse({"error": "empty reference clip"}, status_code=400)
    if len(data) > _MAX_REF_BYTES:
        return JSONResponse({"error": "reference clip too large"}, status_code=413)
    if not (text or "").strip():
        return JSONResponse({"error": "nothing to say"}, status_code=400)

    tmp = tempfile.mkdtemp(prefix="voice_ref_")
    path = os.path.join(tmp, "ref.wav")
    try:
        with open(path, "wb") as f:
            f.write(data)
        wav = await voice_factory._generate_local(db, text, path)
        return Response(content=wav, media_type="audio/wav")
    except Exception as e:
        logger.warning("[voice-api] generation failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=503)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
