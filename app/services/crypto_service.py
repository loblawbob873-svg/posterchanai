"""
Encryption service for sensitive data at rest.
Uses Fernet symmetric encryption from cryptography library.
"""
import os
import base64
import logging
from pathlib import Path
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Key file location (in project root, not in repo)
KEY_FILE = Path(__file__).parent.parent.parent / ".encryption_key"

_fernet: Optional[Fernet] = None


def _get_or_create_key() -> bytes:
    """Get encryption key from environment or file, or create new one."""
    # First, check environment variable
    env_key = os.environ.get("POSTERCHANAI_ENCRYPTION_KEY")
    if env_key:
        try:
            # Validate it's a valid Fernet key
            key = env_key.encode() if isinstance(env_key, str) else env_key
            Fernet(key)  # Validate
            logger.info("Using encryption key from environment")
            return key
        except Exception as e:
            logger.error(f"Invalid POSTERCHANAI_ENCRYPTION_KEY: {e}")

    # Second, try to load from file
    if KEY_FILE.exists():
        try:
            key = KEY_FILE.read_bytes().strip()
            Fernet(key)  # Validate
            logger.info(f"Loaded encryption key from {KEY_FILE}")
            return key
        except Exception as e:
            logger.error(f"Invalid key in {KEY_FILE}: {e}")

    # Generate new key
    key = Fernet.generate_key()
    try:
        KEY_FILE.write_bytes(key)
        KEY_FILE.chmod(0o600)  # Owner read/write only
        logger.info(f"Generated new encryption key, saved to {KEY_FILE}")
    except Exception as e:
        logger.warning(f"Could not save encryption key to file: {e}")
        logger.warning("Key will be regenerated on restart - encrypted data will be lost!")

    return key


def get_fernet() -> Fernet:
    """Get or create the Fernet instance."""
    global _fernet
    if _fernet is None:
        key = _get_or_create_key()
        _fernet = Fernet(key)
    return _fernet


def encrypt_string(plaintext: str) -> str:
    """
    Encrypt a string and return base64-encoded ciphertext.
    Returns prefixed string to identify encrypted values.
    """
    if not plaintext:
        return plaintext

    fernet = get_fernet()
    encrypted = fernet.encrypt(plaintext.encode('utf-8'))
    # Prefix with 'enc:' to identify encrypted values
    return f"enc:{encrypted.decode('utf-8')}"


def decrypt_string(ciphertext: str) -> str:
    """
    Decrypt a string. Handles both encrypted (enc: prefix) and legacy plaintext.
    Returns the original string if decryption fails or not encrypted.
    """
    if not ciphertext:
        return ciphertext

    # Check if this is an encrypted value
    if not ciphertext.startswith("enc:"):
        # Legacy plaintext password - return as-is
        return ciphertext

    try:
        fernet = get_fernet()
        encrypted_part = ciphertext[4:]  # Remove 'enc:' prefix
        decrypted = fernet.decrypt(encrypted_part.encode('utf-8'))
        return decrypted.decode('utf-8')
    except InvalidToken:
        logger.error("Failed to decrypt value - invalid token or wrong key")
        # Return empty to prevent using corrupted data
        return ""
    except Exception as e:
        logger.error(f"Decryption error: {e}")
        return ""


def is_encrypted(value: str) -> bool:
    """Check if a value is encrypted (has enc: prefix)."""
    return value.startswith("enc:") if value else False
