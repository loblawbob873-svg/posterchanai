"""Radicale auth plugin: this node's accounts, with a CalDAV-only app password.

A calendar client cannot sign a Nostr event and cannot carry a session cookie — it speaks HTTP Basic
and nothing else. So the account it logs in with is the app account, and the secret is a **separate
password, used only here**, which the user generates in the client (Settings → Calendar) and pastes
into their phone. Reasons it is not the login password:

  * A phone stores it forever, in plain form, and syncs it to a vendor cloud. That is a fine place
    for a revocable per-device secret and a terrible one for the password that owns the account.
  * Most accounts here have NO password at all — they signed in with a Nostr key — so there would be
    nothing to check.
  * Revoking it must not log the person out of everything else.

Stored as a PBKDF2 hash in the user's own settings, so it is not readable back out of the database,
and compared in constant time.
"""
import hashlib
import hmac
import logging
import os

from radicale.auth import BaseAuth

logger = logging.getLogger(__name__)

SETTING_KEY = "caldav_password"      # per-user, holds "pbkdf2_sha256$<iter>$<salt>$<hash>"
_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, want = (stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), want)
    except Exception:
        return False


class Auth(BaseAuth):
    def login(self, login: str, password: str) -> str:
        """Return the username on success, "" on failure. Radicale calls this per request."""
        if not login or not password:
            return ""
        from app.database import SessionLocal
        from app.models import User, UserSetting
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == login).first()
            if not user:
                # Also accept the email, which is what people type into a phone without thinking.
                user = db.query(User).filter(User.email == login).first()
            if not user:
                return ""
            row = db.query(UserSetting).filter(UserSetting.user_id == user.id,
                                               UserSetting.key == SETTING_KEY).first()
            if not row or not row.value:
                return ""
            if not verify_password(password, row.value):
                logger.info("[caldav] bad password for %s", login)
                return ""
            return user.username
        except Exception as e:
            logger.warning("[caldav] login error for %s: %s", login, e)
            return ""
        finally:
            db.close()
