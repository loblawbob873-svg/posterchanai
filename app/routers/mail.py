"""
Mail Router - API endpoints for email functionality.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user
from app.models import User
from app.services.mail_service import get_attachment, get_user_mail_accounts

router = APIRouter(prefix="/api/mail", tags=["mail"])


@router.get("/attachment/{account_hint}/{uid}/{index}")
async def download_attachment(
    account_hint: str,
    uid: str,
    index: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Download an email attachment."""
    # Find the full account email from hint
    accounts = get_user_mail_accounts(current_user.id, db)
    account_email = None
    for acc in accounts:
        if account_hint.lower() in acc.email.lower():
            account_email = acc.email
            break

    if not account_email:
        raise HTTPException(status_code=404, detail="Account not found")

    attachment = get_attachment(current_user.id, db, account_email, uid, index)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    return Response(
        content=attachment.data,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.filename}"'
        }
    )
