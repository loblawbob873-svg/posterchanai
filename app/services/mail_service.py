"""
Mail Service - IMAP/SMTP email integration.

Provides:
- IMAP inbox checking across multiple accounts
- Email viewing with attachments
- Reply/Reply-all functionality
- Send new emails with attachments
- Delete messages
"""

import asyncio
import base64
import email
import imaplib
import json
import logging
import re
import smtplib
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from email import encoders
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, parseaddr
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models import UserSetting
from app.services.crypto_service import decrypt_string, encrypt_string

logger = logging.getLogger(__name__)


@dataclass
class EmailAttachment:
    """Represents an email attachment."""

    filename: str
    content_type: str
    size: int
    data: bytes = field(repr=False)  # Don't print binary data


@dataclass
class EmailMessage:
    """Represents an email message."""

    uid: str
    account: str  # Which account this came from
    subject: str
    sender: str
    sender_email: str
    to: str
    date: datetime
    body_text: str
    body_html: Optional[str]
    attachments: List[EmailAttachment] = field(default_factory=list)
    is_read: bool = True
    message_id: str = ""
    references: str = ""
    in_reply_to: str = ""


@dataclass
class MailAccount:
    """User's mail account configuration."""

    email: str
    password: str
    imap_server: str
    imap_port: int = 993
    smtp_server: str = ""
    smtp_port: int = 587
    use_ssl: bool = True


def get_user_mail_accounts(user_id: int, db: Session) -> List[MailAccount]:
    """Get user's configured mail accounts. Passwords are decrypted automatically."""
    setting = db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == "mail_accounts").first()

    if not setting or not setting.value:
        return []

    try:
        accounts_data = json.loads(setting.value)
        accounts = []
        for acc in accounts_data:
            # Decrypt password (handles both encrypted and legacy plaintext)
            acc["password"] = decrypt_string(acc.get("password", ""))
            accounts.append(MailAccount(**acc))
        return accounts
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Invalid mail_accounts JSON for user {user_id}: {e}")
        return []


def save_user_mail_accounts(user_id: int, accounts: List[MailAccount], db: Session):
    """Save user's mail accounts. Passwords are encrypted before storage."""
    setting = db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == "mail_accounts").first()

    accounts_data = [
        {
            "email": acc.email,
            "password": encrypt_string(acc.password),  # Encrypt password
            "imap_server": acc.imap_server,
            "imap_port": acc.imap_port,
            "smtp_server": acc.smtp_server,
            "smtp_port": acc.smtp_port,
            "use_ssl": acc.use_ssl,
        }
        for acc in accounts
    ]

    if setting:
        setting.value = json.dumps(accounts_data)
    else:
        setting = UserSetting(user_id=user_id, key="mail_accounts", value=json.dumps(accounts_data))
        db.add(setting)

    db.commit()


def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename for use in HTTP headers and MIME parts.
    Removes/replaces characters that could cause header injection or path traversal.
    """
    if not filename:
        return "attachment"

    # Remove path components (prevent path traversal)
    filename = filename.replace("\\", "/").split("/")[-1]

    # Remove characters that could break headers or cause issues
    # Remove: quotes, newlines, carriage returns, null bytes
    dangerous_chars = ['"', "'", "\n", "\r", "\x00", "\t"]
    for char in dangerous_chars:
        filename = filename.replace(char, "_")

    # Limit length
    if len(filename) > 200:
        name, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
        filename = name[:195] + ("." + ext if ext else "")

    # Ensure we have a valid filename
    filename = filename.strip()
    if not filename or filename in (".", ".."):
        filename = "attachment"

    return filename


def html_to_text(html: str) -> str:
    """Convert HTML to readable plain text."""
    import html as html_module
    import re

    if not html:
        return ""

    text = html

    # Replace common block elements with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<hr\s*/?>", "\n---\n", text, flags=re.IGNORECASE)

    # Extract link URLs: <a href="url">text</a> -> [text](url) for clickable markdown
    # Handle nested tags by extracting inner text more carefully
    def replace_link(m):
        url = m.group(1) or m.group(2) or m.group(3)  # Try different quote styles
        inner = m.group(4)
        if not url:
            return m.group(0)  # Return original if no URL found
        # Strip any remaining HTML tags from inner text
        inner_text = re.sub(r"<[^>]+>", "", inner).strip()
        # If no text, use URL as text
        if not inner_text:
            inner_text = url
        # Clean up URL - decode HTML entities like &amp; -> &
        url = url.strip()
        url = html_module.unescape(url)
        # Also decode inner text entities
        inner_text = html_module.unescape(inner_text)
        return f"[{inner_text}]({url})"

    # Match href with double quotes, single quotes, or no quotes
    # Use [\s\S] instead of [^>] to handle newlines in tag attributes
    text = re.sub(
        r'<a(?:\s[\s\S]*?)href\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))[\s\S]*?>([\s\S]*?)</a>',
        replace_link,
        text,
        flags=re.IGNORECASE,
    )

    # Remove style and script content entirely
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)

    # Remove CSS rules that leak through (various patterns)
    # Pattern: .className { ... } or #idName { ... }
    text = re.sub(r"[.#][a-zA-Z_][a-zA-Z0-9_-]*\s*\{[^}]*\}", "", text, flags=re.DOTALL)
    # Pattern: tagName.className { ... } or tagName#id { ... }
    text = re.sub(r"[a-zA-Z_][a-zA-Z0-9_-]*[.#][a-zA-Z0-9_-]*\s*\{[^}]*\}", "", text, flags=re.DOTALL)
    # Pattern: selector selector { ... } (e.g., "tr.Bordered td")
    text = re.sub(r"[a-zA-Z_][a-zA-Z0-9_.-]*\s+[a-zA-Z_][a-zA-Z0-9_.-]*\s*\{[^}]*\}", "", text, flags=re.DOTALL)
    # Pattern: table, tr, td { ... } (CSS block without class/id)
    text = re.sub(
        r"\b(?:table|tr|td|th|div|span|p|a|img|body|html)\s*\{[^}]*\}", "", text, flags=re.IGNORECASE | re.DOTALL
    )

    # Remove all remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)

    # Decode HTML entities
    text = html_module.unescape(text)

    # Clean up whitespace
    text = re.sub(r"[ \t]+", " ", text)  # Multiple spaces to single
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)  # Multiple newlines to double
    text = "\n".join(line.strip() for line in text.split("\n"))  # Strip each line

    return text.strip()


def decode_mime_header(header_value: str) -> str:
    """Decode a MIME-encoded header value."""
    if not header_value:
        return ""

    decoded_parts = []
    for part, encoding in decode_header(header_value):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(encoding or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(part)

    return "".join(decoded_parts)


def get_email_body(msg: email.message.Message) -> Tuple[str, Optional[str]]:
    """Extract text and HTML body from email message."""
    text_body = ""
    html_body = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            # Skip attachments
            if "attachment" in content_disposition:
                continue

            if content_type == "text/plain":
                try:
                    charset = part.get_content_charset() or "utf-8"
                    text_body = part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    text_body = str(part.get_payload())

            elif content_type == "text/html":
                try:
                    charset = part.get_content_charset() or "utf-8"
                    html_body = part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    html_body = str(part.get_payload())
    else:
        content_type = msg.get_content_type()
        try:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
                decoded = payload.decode(charset, errors="replace")
                if content_type == "text/html":
                    html_body = decoded
                else:
                    text_body = decoded
        except Exception:
            text_body = str(msg.get_payload())

    return text_body, html_body


# No attachment size limits
MAX_ATTACHMENT_SIZE = None  # No limit
MAX_TOTAL_ATTACHMENT_SIZE = None  # No limit


def get_attachments(msg: email.message.Message) -> List[EmailAttachment]:
    """Extract attachments from email message with size limits."""
    attachments = []
    total_size = 0

    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))

        if "attachment" in content_disposition or part.get_filename():
            filename = part.get_filename()
            if filename:
                filename = decode_mime_header(filename)

                try:
                    data = part.get_payload(decode=True) or b""
                    size = len(data)

                    # No size limits - load all attachments

                    total_size += size
                    attachments.append(
                        EmailAttachment(filename=filename, content_type=part.get_content_type(), size=size, data=data)
                    )
                except Exception as e:
                    logger.debug(f"Error extracting attachment {filename}: {e}")

    return attachments


def parse_email(raw_email: bytes, uid: str, account_email: str) -> Optional[EmailMessage]:
    """Parse raw email bytes into an EmailMessage object."""
    try:
        msg = email.message_from_bytes(raw_email)

        # Parse date
        date_str = msg.get("Date", "")
        try:
            from datetime import timezone
            from email.utils import parsedate_to_datetime

            msg_date = parsedate_to_datetime(date_str)
            # Ensure timezone-aware (some emails might have naive datetimes)
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
        except Exception:
            from datetime import timezone

            msg_date = datetime.now(timezone.utc)

        # Parse sender
        sender_raw = msg.get("From", "")
        sender_name, sender_email_addr = parseaddr(sender_raw)
        sender_name = decode_mime_header(sender_name) or sender_email_addr

        # Get body and attachments
        text_body, html_body = get_email_body(msg)
        attachments = get_attachments(msg)

        return EmailMessage(
            uid=uid,
            account=account_email,
            subject=decode_mime_header(msg.get("Subject", "(No Subject)")),
            sender=sender_name,
            sender_email=sender_email_addr,
            to=decode_mime_header(msg.get("To", "")),
            date=msg_date,
            body_text=text_body,
            body_html=html_body,
            attachments=attachments,
            is_read=True,  # Can't determine from RFC822 alone
            message_id=msg.get("Message-ID", ""),
            references=msg.get("References", ""),
            in_reply_to=msg.get("In-Reply-To", ""),
        )
    except Exception as e:
        logger.error(f"Error parsing email {uid}: {e}")
        return None


def validate_mail_server(hostname: str, port: int) -> bool:
    """
    Validate mail server address to prevent SSRF attacks.
    Blocks internal IPs, localhost, cloud metadata endpoints, etc.
    """
    import ipaddress
    import socket

    # Block obvious localhost references
    blocked_hostnames = {"localhost", "localhost.localdomain", "127.0.0.1", "0.0.0.0", "::1"}
    if hostname.lower() in blocked_hostnames:
        logger.warning(f"SSRF blocked: localhost reference {hostname}")
        return False

    # Resolve hostname to IP and validate
    try:
        ip_str = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(ip_str)

        # Block private/reserved ranges
        if ip.is_private:
            logger.warning(f"SSRF blocked: private IP {ip_str} for {hostname}")
            return False
        if ip.is_loopback:
            logger.warning(f"SSRF blocked: loopback IP {ip_str} for {hostname}")
            return False
        if ip.is_link_local:
            logger.warning(f"SSRF blocked: link-local IP {ip_str} for {hostname}")
            return False
        if ip.is_reserved:
            logger.warning(f"SSRF blocked: reserved IP {ip_str} for {hostname}")
            return False

        # Block AWS/cloud metadata endpoint specifically
        if ip_str == "169.254.169.254":
            logger.warning(f"SSRF blocked: cloud metadata endpoint {hostname}")
            return False

        return True
    except socket.gaierror as e:
        logger.error(f"DNS resolution failed for {hostname}: {e}")
        return False
    except ValueError as e:
        logger.error(f"Invalid IP address for {hostname}: {e}")
        return False


# Connection timeout in seconds
MAIL_CONNECTION_TIMEOUT = 10  # Reduced to prevent long hangs
MAIL_OPERATION_TIMEOUT = 8  # Timeout for individual IMAP operations
MAIL_TOTAL_TIMEOUT = 20  # Maximum total time for mail operations

# Thread pool for running blocking IMAP operations with timeout
_mail_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="mail_worker")


def run_with_timeout(func, timeout=MAIL_OPERATION_TIMEOUT, *args, **kwargs):
    """Run a blocking function with timeout using thread pool."""
    try:
        future = _mail_executor.submit(func, *args, **kwargs)
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        logger.warning(f"Operation {func.__name__} timed out after {timeout}s")
        return None
    except Exception as e:
        logger.error(f"Operation {func.__name__} failed: {e}")
        return None


def connect_imap(account: MailAccount) -> Optional[imaplib.IMAP4_SSL]:
    """Connect to IMAP server with SSRF protection and timeout."""
    # Validate server address first (SSRF protection)
    if not validate_mail_server(account.imap_server, account.imap_port):
        logger.error(f"IMAP connection blocked for {account.email}: invalid server address")
        return None

    try:
        if account.use_ssl:
            imap = imaplib.IMAP4_SSL(account.imap_server, account.imap_port, timeout=MAIL_CONNECTION_TIMEOUT)
        else:
            imap = imaplib.IMAP4(account.imap_server, account.imap_port, timeout=MAIL_CONNECTION_TIMEOUT)

        # Set socket timeout for all subsequent operations
        imap.socket().settimeout(MAIL_CONNECTION_TIMEOUT)

        imap.login(account.email, account.password)
        return imap
    except Exception as e:
        logger.error(f"IMAP connection failed for {account.email}: {e}")
        return None


def fetch_messages(
    account: MailAccount, folder: str = "INBOX", limit: int = 20, unread_only: bool = False
) -> List[EmailMessage]:
    """Fetch messages from an IMAP account."""
    messages = []

    imap = connect_imap(account)
    if not imap:
        return messages

    try:
        imap.select('"' + folder + '"')

        # Search criteria - use UID SEARCH to get actual UIDs
        if unread_only:
            status, data = imap.uid("search", None, "UNSEEN")
        else:
            status, data = imap.uid("search", None, "ALL")

        if status != "OK":
            return messages

        # Get message UIDs (newest first)
        uids = data[0].split()
        logger.info(
            f"fetch_messages: found {len(uids)} UIDs, sample: {[u.decode() if isinstance(u, bytes) else u for u in uids[-5:]]}"
        )
        uids = uids[-limit:] if len(uids) > limit else uids
        uids.reverse()  # Newest first

        for uid in uids:
            try:
                uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
                # Use UID FETCH to get by actual UID
                status, msg_data = imap.uid("fetch", uid_str, "(RFC822 FLAGS)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                # Handle response format
                if not isinstance(msg_data[0], tuple) or len(msg_data[0]) < 2:
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Parse flags from response
                flags_data = msg_data[0][0].decode() if isinstance(msg_data[0][0], bytes) else str(msg_data[0][0])
                is_read = "\\Seen" in flags_data

                # Parse date
                date_str = msg.get("Date", "")
                try:
                    from datetime import timezone
                    from email.utils import parsedate_to_datetime

                    msg_date = parsedate_to_datetime(date_str)
                    # Ensure timezone-aware (some emails might have naive datetimes)
                    if msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=timezone.utc)
                except Exception:
                    from datetime import timezone

                    msg_date = datetime.now(timezone.utc)

                # Parse sender
                sender_raw = msg.get("From", "")
                sender_name, sender_email = parseaddr(sender_raw)
                sender_name = decode_mime_header(sender_name) or sender_email

                # Get body and attachments
                text_body, html_body = get_email_body(msg)
                attachments = get_attachments(msg)

                messages.append(
                    EmailMessage(
                        uid=uid_str,
                        account=account.email,
                        subject=decode_mime_header(msg.get("Subject", "(No Subject)")),
                        sender=sender_name,
                        sender_email=sender_email,
                        to=decode_mime_header(msg.get("To", "")),
                        date=msg_date,
                        body_text=text_body,
                        body_html=html_body,
                        attachments=attachments,
                        is_read=is_read,
                        message_id=msg.get("Message-ID", ""),
                        references=msg.get("References", ""),
                        in_reply_to=msg.get("In-Reply-To", ""),
                    )
                )

            except Exception as e:
                logger.debug(f"Error parsing message {uid}: {e}")
                continue

    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return messages


def fetch_all_accounts(
    user_id: int, db: Session, limit_per_account: int = 10, unread_only: bool = False
) -> List[EmailMessage]:
    """Fetch messages from all user's accounts with timeout protection."""
    accounts = get_user_mail_accounts(user_id, db)
    all_messages = []

    for account in accounts:
        try:
            # Run with timeout to prevent one slow account from blocking everything
            def fetch_account():
                return fetch_messages(account, limit=limit_per_account, unread_only=unread_only)

            messages = run_with_timeout(fetch_account, timeout=MAIL_OPERATION_TIMEOUT)
            if messages:
                all_messages.extend(messages)
            else:
                logger.warning(f"Skipping {account.email} - fetch timed out or failed")
        except Exception as e:
            logger.error(f"Error fetching from {account.email}: {e}")
            # Continue to next account instead of failing completely

    # Sort by date (newest first)
    all_messages.sort(key=lambda m: m.date, reverse=True)
    return all_messages


def list_folders(user_id: int, db: Session, account_email: str) -> List[str]:
    """List all IMAP folders for an account."""
    accounts = get_user_mail_accounts(user_id, db)

    for account in accounts:
        if account.email == account_email:
            imap = connect_imap(account)
            if not imap:
                return []

            try:
                # Wrap imap.list() with timeout protection
                def list_imap_folders():
                    return imap.list()

                result = run_with_timeout(list_imap_folders, timeout=5)
                if not result or result[0] != "OK":
                    logger.error(f"Failed to list folders or timed out")
                    return []

                status, folder_data = result

                folders = []
                for folder_line in folder_data:
                    try:
                        decoded = folder_line.decode() if isinstance(folder_line, bytes) else folder_line
                        # Extract folder name from IMAP LIST response
                        match = re.search(r'"([^"]+)"$|(\S+)$', decoded)
                        if match:
                            folder_name = match.group(1) or match.group(2)
                            # Skip special folders that can't be selected
                            if folder_name.lower() not in ("[gmail]", "[google mail]"):
                                folders.append(folder_name)
                    except Exception as e:
                        logger.debug(f"Error parsing folder line: {folder_line} - {e}")
                        continue

                return sorted(folders)
            except Exception as e:
                logger.error(f"Error listing folders: {e}")
                return []
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

    return []


def format_folder_list(folders: List[str], account_email: str) -> str:
    """Format folder list for display with browse buttons."""
    if not folders:
        return "No folders found."

    account_short = account_email.split("@")[0]

    lines = [f"## ◈ FOLDERS ({account_email}) ◈\n"]

    for folder in folders:
        # Show folder with browse button
        browse_cmd = f"mail folder {account_short} {folder}"
        lines.append(f"- **{folder}** [Browse](cmd:{browse_cmd})")

    return "\n".join(lines)


def search_messages(user_id: int, db: Session, account_email: str, query: str, limit: int = 20) -> List[EmailMessage]:
    """Search messages in all folders of an account using IMAP SEARCH."""
    accounts = get_user_mail_accounts(user_id, db)
    messages = []

    for account in accounts:
        if account.email == account_email:
            imap = connect_imap(account)
            if not imap:
                return []

            try:
                # Get list of all folders
                status, folder_data = imap.list()
                if status != "OK":
                    logger.error(f"Failed to list folders: {status}")
                    return []

                folders = []
                for folder_line in folder_data:
                    # Parse folder name from IMAP LIST response
                    # Format varies: (\\flags) "." "INBOX" or (\\flags) "/" "Folder"
                    try:
                        decoded = folder_line.decode() if isinstance(folder_line, bytes) else folder_line
                        # Try to extract folder name - it's usually the last quoted string
                        import re

                        # Match the last quoted string or unquoted name at end
                        match = re.search(r'"([^"]+)"$|(\S+)$', decoded)
                        if match:
                            folder_name = match.group(1) or match.group(2)
                            # Skip special folders that can't be searched
                            if folder_name.lower() not in ("[gmail]", "[google mail]", "straps"):
                                folders.append(folder_name)
                    except Exception as e:
                        logger.debug(f"Error parsing folder line: {folder_line} - {e}")
                        continue

                logger.info(f"Found folders: {folders}")
                logger.info(f"Searching {len(folders)} folders ({len(query or '')}-char query)")

                for folder in folders:
                    try:
                        status, _ = imap.select('"' + folder + '"', readonly=True)
                        if status != "OK":
                            logger.debug(f"Could not select folder: {folder}")
                            continue

                        # Search multiple criteria and combine results using UID commands
                        all_uids = set()
                        for criteria in [f'FROM "{query}"', f'TO "{query}"', f'SUBJECT "{query}"']:
                            try:
                                # Use UID SEARCH to get actual UIDs
                                status, data = imap.uid("search", None, criteria)
                                if status == "OK" and data[0]:
                                    all_uids.update(data[0].split())
                            except Exception as e:
                                logger.debug(f"Search error for {criteria}: {e}")

                        logger.info(f"Search in {folder}: {len(all_uids)} results")
                        if not all_uids:
                            continue

                        uids = list(all_uids)
                        logger.info(
                            f"Fetching up to {min(limit, len(uids))} messages from {folder}, UIDs sample: {uids[:3]}"
                        )
                        # Get most recent matches first
                        fetched = 0
                        uids_to_fetch = list(reversed(uids[-limit:]))
                        logger.info(f"Will fetch {len(uids_to_fetch)} UIDs: {uids_to_fetch[:5]}...")
                        for uid in uids_to_fetch:
                            if len(messages) >= limit:
                                break
                            try:
                                # Decode UID if bytes
                                uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)

                                # Use UID FETCH to fetch by UID
                                status, msg_data = imap.uid("fetch", uid_str, "(RFC822)")
                                logger.info(
                                    f"UID {uid_str}: status={status}, type={type(msg_data)}, len={len(msg_data) if msg_data else 0}, first={type(msg_data[0]) if msg_data and msg_data[0] else None}"
                                )
                                if status != "OK" or not msg_data or not msg_data[0]:
                                    continue

                                # Handle different response formats
                                if isinstance(msg_data[0], tuple) and len(msg_data[0]) >= 2:
                                    raw_email = msg_data[0][1]
                                    logger.info(
                                        f"UID {uid_str}: tuple[0] len={len(msg_data[0])}, raw_email type={type(raw_email)}, size={len(raw_email) if raw_email else 0}"
                                    )
                                elif isinstance(msg_data[0], bytes):
                                    # Some servers return flags then data
                                    if len(msg_data) > 1 and isinstance(msg_data[1], tuple):
                                        raw_email = msg_data[1][1]
                                    else:
                                        continue
                                else:
                                    logger.info(
                                        f"UID {uid_str}: tuple len={len(msg_data[0]) if isinstance(msg_data[0], tuple) else 'N/A'}"
                                    )
                                    continue
                                msg = parse_email(raw_email, uid_str, account.email)
                                logger.info(f"UID {uid_str}: parse_email returned {msg is not None}")
                                if msg:
                                    # Add folder info to message
                                    msg.account = f"{account.email} ({folder})"
                                    messages.append(msg)
                                    fetched += 1
                            except Exception as e:
                                logger.debug(f"Error fetching message {uid}: {e}")
                                continue
                        logger.info(f"Fetched {fetched} messages from {folder}")

                    except Exception as e:
                        logger.error(f"Error searching folder {folder}: {e}")
                        continue

                # Sort by date (newest first)
                messages.sort(key=lambda m: m.date, reverse=True)
                return messages[:limit]

            except Exception as e:
                logger.error(f"Error searching messages: {e}")
                return []
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

    return []


def get_message_by_id(
    user_id: int, db: Session, account_email: str, uid: str, folder: str = None
) -> Optional[EmailMessage]:
    """Get a specific message by account and UID. Searches all folders if not found in INBOX."""
    logger.info(f"get_message_by_id: account={account_email}, uid={uid}, folder={folder}")
    accounts = get_user_mail_accounts(user_id, db)

    for account in accounts:
        if account.email == account_email:
            imap = connect_imap(account)
            if not imap:
                return None

            try:
                # If folder specified, search there first
                folders_to_check = []
                if folder:
                    folders_to_check.append(folder)
                else:
                    # Try INBOX first, then get all folders
                    folders_to_check.append("INBOX")

                # Try specified/INBOX first
                for check_folder in folders_to_check:
                    try:
                        status, _ = imap.select('"' + check_folder + '"', readonly=True)
                        if status == "OK":
                            # Use UID FETCH
                            status, msg_data = imap.uid("fetch", uid, "(RFC822)")
                            if status == "OK" and msg_data and msg_data[0]:
                                if isinstance(msg_data[0], tuple) and len(msg_data[0]) >= 2:
                                    raw_email = msg_data[0][1]
                                    msg = parse_email(raw_email, uid, account.email)
                                    if msg:
                                        msg.account = f"{account.email} ({check_folder})"
                                        return msg
                    except Exception as e:
                        logger.debug(f"Error checking folder {check_folder}: {e}")

                # If not found in INBOX, search all folders
                if not folder:
                    status, folder_data = imap.list()
                    if status == "OK":
                        for folder_line in folder_data:
                            try:
                                decoded = folder_line.decode() if isinstance(folder_line, bytes) else folder_line
                                import re

                                match = re.search(r'"([^"]+)"$|(\S+)$', decoded)
                                if match:
                                    folder_name = match.group(1) or match.group(2)
                                    if folder_name == "INBOX" or folder_name.lower() in ("[gmail]", "[google mail]"):
                                        continue

                                    status, _ = imap.select('"' + folder_name + '"', readonly=True)
                                    if status == "OK":
                                        status, msg_data = imap.uid("fetch", uid, "(RFC822)")
                                        if status == "OK" and msg_data and msg_data[0]:
                                            if isinstance(msg_data[0], tuple) and len(msg_data[0]) >= 2:
                                                raw_email = msg_data[0][1]
                                                msg = parse_email(raw_email, uid, account.email)
                                                if msg:
                                                    msg.account = f"{account.email} ({folder_name})"
                                                    return msg
                            except Exception as e:
                                logger.debug(f"Error searching folder: {e}")
                                continue

            except Exception as e:
                logger.error(f"Error getting message by ID: {e}")
                return None
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

    return None


def delete_message(user_id: int, db: Session, account_email: str, uid: str, folder: str = "INBOX") -> bool:
    """Delete a message by UID from specified folder."""
    accounts = get_user_mail_accounts(user_id, db)

    for account in accounts:
        if account.email == account_email:
            imap = connect_imap(account)
            if not imap:
                return False

            try:
                # Ensure socket timeout is set
                if hasattr(imap, 'socket') and imap.socket():
                    imap.socket().settimeout(MAIL_OPERATION_TIMEOUT)

                # Wrap select with timeout
                def select_folder():
                    return imap.select('"' + folder + '"')
                
                result = run_with_timeout(select_folder, timeout=MAIL_OPERATION_TIMEOUT)
                if not result or result[0] != "OK":
                    logger.error(f"Failed to select folder {folder}")
                    return False

                # Wrap store with timeout
                def store_deleted():
                    return imap.uid("store", uid, "+FLAGS", "\\Deleted")
                
                result = run_with_timeout(store_deleted, timeout=MAIL_OPERATION_TIMEOUT)
                if not result or result[0] != "OK":
                    logger.error(f"Failed to mark message {uid} for deletion")
                    return False

                # Wrap expunge with timeout - this is the operation that often times out
                def expunge_messages():
                    return imap.expunge()
                
                result = run_with_timeout(expunge_messages, timeout=MAIL_OPERATION_TIMEOUT)
                if result is None:
                    logger.warning(f"Expunge timed out for message {uid}, but message was marked for deletion")
                    # Message is still marked for deletion, so consider it a partial success
                    # The server will expunge it eventually or on next connection
                
                logger.info(f"Deleted message {uid} from {folder} in {account_email}")
                return True
            except Exception as e:
                logger.error(f"Error deleting message: {e}")
                return False
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

    return False


def delete_all_messages(user_id: int, db: Session, account_email: str) -> int:
    """Delete ALL messages in the INBOX for the specified account. Returns count, or -1 on error."""
    accounts = get_user_mail_accounts(user_id, db)

    for account in accounts:
        if account.email == account_email:
            imap = connect_imap(account)
            if not imap:
                return -1

            try:
                # Ensure socket timeout is set
                if hasattr(imap, 'socket') and imap.socket():
                    imap.socket().settimeout(MAIL_OPERATION_TIMEOUT)

                # Wrap select with timeout
                def select_inbox():
                    return imap.select("INBOX")
                
                result = run_with_timeout(select_inbox, timeout=MAIL_OPERATION_TIMEOUT)
                if not result or result[0] != "OK":
                    logger.error("Failed to select INBOX")
                    return -1

                # Wrap search with timeout
                def search_all():
                    return imap.search(None, "ALL")
                
                result = run_with_timeout(search_all, timeout=MAIL_OPERATION_TIMEOUT)
                if not result or result[0] != "OK":
                    return -1

                status, data = result
                message_ids = data[0].split()
                if not message_ids:
                    return 0

                count = len(message_ids)

                # Mark all for deletion with timeout protection
                for uid in message_ids:
                    # Create a closure that captures the uid value
                    def make_store_func(u):
                        def store_deleted():
                            return imap.store(u, "+FLAGS", "\\Deleted")
                        return store_deleted
                    run_with_timeout(make_store_func(uid), timeout=MAIL_OPERATION_TIMEOUT)

                # Wrap expunge with timeout - this is the operation that often times out
                def expunge_messages():
                    return imap.expunge()
                
                result = run_with_timeout(expunge_messages, timeout=MAIL_TOTAL_TIMEOUT)
                if result is None:
                    logger.warning(f"Expunge timed out for {count} messages, but messages were marked for deletion")
                    # Messages are still marked for deletion, so consider it a partial success
                
                logger.info(f"Purged {count} messages from {account_email} inbox")
                return count

            except Exception as e:
                logger.error(f"Error purging inbox: {e}")
                return -1
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

    return -1


def archive_message(user_id: int, db: Session, account_email: str, uid: str, folder: str = "INBOX") -> bool:
    """Archive a message by moving to INBOX.Archive folder."""
    accounts = get_user_mail_accounts(user_id, db)

    for account in accounts:
        if account.email == account_email:
            imap = connect_imap(account)
            if not imap:
                return False

            try:
                status, _ = imap.select('"' + folder + '"')
                if status != "OK":
                    logger.error(f"Failed to select folder {folder}")
                    return False

                # Verify message exists
                status, data = imap.uid("SEARCH", None, f"UID {uid}")
                if status != "OK" or not data[0]:
                    logger.error(f"Message UID {uid} not found in {folder}")
                    return False

                # Use INBOX.Archive as the standard archive folder
                archive_folder = "INBOX.Archive"

                # Check if it exists, create if not (with timeout)
                def list_folders_sync():
                    return imap.list()

                result = run_with_timeout(list_folders_sync, timeout=5)
                folder_exists = False
                if result and result[0] == "OK":
                    status, folders = result
                    for folder_line in folders:
                        decoded = folder_line.decode() if isinstance(folder_line, bytes) else folder_line
                        if "INBOX.Archive" in decoded or '"INBOX.Archive"' in decoded:
                            folder_exists = True
                            break

                if not folder_exists:
                    result = imap.create(archive_folder)
                    if result[0] != "OK":
                        # Try without INBOX prefix as fallback
                        archive_folder = "Archive"
                        imap.create(archive_folder)
                    logger.info(f"Created {archive_folder} folder for {account_email}")

                # Copy to archive folder using UID command (not sequence number)
                result = imap.uid("COPY", uid, archive_folder)
                if result[0] != "OK":
                    logger.error(f"Failed to copy message UID {uid} to {archive_folder}: {result}")
                    return False

                # Delete from source folder using UID command with timeout protection
                def store_deleted():
                    return imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
                
                result = run_with_timeout(store_deleted, timeout=MAIL_OPERATION_TIMEOUT)
                if not result or result[0] != "OK":
                    logger.warning(f"Failed to mark message {uid} for deletion after archiving")
                    # Still return True since copy succeeded
                
                # Wrap expunge with timeout - this is the operation that often times out
                def expunge_messages():
                    return imap.expunge()
                
                result = run_with_timeout(expunge_messages, timeout=MAIL_OPERATION_TIMEOUT)
                if result is None:
                    logger.warning(f"Expunge timed out for archived message {uid}, but message was marked for deletion")

                logger.info(f"Archived message {uid} from {account_email}:{folder} to {archive_folder}")
                return True

            except Exception as e:
                logger.error(f"Error archiving message: {e}")
                return False
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass

    return False


def send_email(
    account: MailAccount,
    to: str,
    subject: str,
    body: str,
    html_body: Optional[str] = None,
    attachments: List[Tuple[str, bytes, str]] = None,  # (filename, data, content_type)
    reply_to_msg: Optional[EmailMessage] = None,
    cc: str = "",
    bcc: str = "",
    inline_images: List[Tuple[str, bytes, str]] = None,  # (cid, data, content_type) for cid: refs in html_body
) -> bool:
    """Send an email via SMTP."""
    try:
        # Create message
        if attachments or html_body or inline_images:
            msg = MIMEMultipart("mixed")

            # Add body
            if html_body:
                body_part = MIMEMultipart("alternative")
                body_part.attach(MIMEText(body, "plain", "utf-8"))
                body_part.attach(MIMEText(html_body, "html", "utf-8"))
                msg.attach(body_part)
            else:
                msg.attach(MIMEText(body, "plain", "utf-8"))

            # Inline images referenced from the HTML via cid: (e.g. a branding logo).
            if inline_images:
                from email.mime.image import MIMEImage
                for cid, idata, imime in inline_images:
                    if not idata:
                        continue
                    try:
                        sub = imime.split("/", 1)[1] if "/" in (imime or "") else "png"
                        img = MIMEImage(idata, _subtype=sub)
                        img.add_header("Content-ID", f"<{cid}>")
                        img.add_header("Content-Disposition", "inline", filename=f"{cid}.{sub}")
                        msg.attach(img)
                    except Exception as e:
                        logger.warning(f"Inline image {cid} failed: {e}")

            # Add attachments with size limits and filename sanitization
            if attachments:
                total_size = 0
                attachment_count = 0
                for filename, data, content_type in attachments:
                    # Skip if data is None or empty
                    if not data:
                        logger.warning(f"Skipping attachment {filename}: empty data")
                        continue
                    
                    # Validate data is bytes
                    if not isinstance(data, bytes):
                        logger.warning(f"Attachment {filename} is not bytes, converting...")
                        try:
                            if isinstance(data, str):
                                data = data.encode('utf-8')
                            else:
                                data = bytes(data)
                        except Exception as e:
                            logger.error(f"Failed to convert attachment {filename} to bytes: {e}")
                            continue
                    
                    # No size limits - include all attachments
                    total_size += len(data)
                    safe_filename = sanitize_filename(filename)
                    
                    # Validate and parse content_type
                    if not content_type or not isinstance(content_type, str):
                        logger.warning(f"Invalid content_type for {filename}, defaulting to application/octet-stream")
                        content_type = "application/octet-stream"
                    
                    try:
                        # Parse content_type (e.g., "image/png" -> ("image", "png"))
                        if "/" in content_type:
                            main_type, sub_type = content_type.split("/", 1)
                        else:
                            # Fallback for invalid content types
                            logger.warning(f"Invalid content_type format '{content_type}' for {filename}, using application/octet-stream")
                            main_type, sub_type = "application", "octet-stream"
                        
                        part = MIMEBase(main_type, sub_type)
                        part.set_payload(data)
                        encoders.encode_base64(part)
                        
                        # Use RFC 2231 encoding for filenames with non-ASCII characters
                        # This ensures proper handling of Unicode filenames
                        try:
                            safe_filename.encode('ascii')
                            # ASCII filename - use simple format
                            part.add_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
                        except UnicodeEncodeError:
                            # Non-ASCII filename - use RFC 2231 encoding
                            from email.header import Header
                            encoded_filename = Header(safe_filename, 'utf-8').encode()
                            part.add_header("Content-Disposition", f'attachment; filename*=UTF-8\'\'{encoded_filename}')
                        
                        msg.attach(part)
                        attachment_count += 1
                        logger.debug(f"Attached {filename} ({len(data)} bytes, {content_type})")
                    except Exception as e:
                        logger.error(f"Error attaching {filename}: {e}", exc_info=True)
                        # Continue with other attachments
                        continue
                
                if attachment_count > 0:
                    logger.info(f"Added {attachment_count} attachment(s) to email (total size: {total_size:,} bytes)")
                else:
                    logger.warning("No attachments were successfully added to email")
        else:
            msg = MIMEText(body, "plain", "utf-8")

        # Set headers - sanitize recipient to prevent header injection
        # Remove newlines and carriage returns from recipient (email headers can't contain these)
        to_clean = to.replace("\n", " ").replace("\r", "").strip()
        msg["From"] = account.email
        msg["To"] = to_clean
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)

        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc

        # Threading headers for replies
        if reply_to_msg:
            if reply_to_msg.message_id:
                msg["In-Reply-To"] = reply_to_msg.message_id
                refs = reply_to_msg.references
                if refs:
                    msg["References"] = f"{refs} {reply_to_msg.message_id}"
                else:
                    msg["References"] = reply_to_msg.message_id

        # Connect and send
        smtp_server = account.smtp_server or account.imap_server
        smtp_port = account.smtp_port or 587

        # SSRF protection
        if not validate_mail_server(smtp_server, smtp_port):
            logger.error(f"SMTP connection blocked: invalid server address {smtp_server}")
            return False

        smtp = None
        try:
            # Establish SMTP connection with timeout protection
            logger.debug(f"Connecting to SMTP server {smtp_server}:{smtp_port}")
            if smtp_port == 465:
                smtp = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=MAIL_CONNECTION_TIMEOUT)
            else:
                smtp = smtplib.SMTP(smtp_server, smtp_port, timeout=MAIL_CONNECTION_TIMEOUT)
                # Start TLS - this can fail if server doesn't support it
                smtp.starttls()
            
            logger.debug(f"SMTP connection established, authenticating as {account.email}")
            # Login with timeout protection
            smtp.login(account.email, account.password)
            logger.debug("SMTP authentication successful")

            # Collect all recipients
            recipients = [to_clean]
            if cc:
                recipients.extend([addr.strip() for addr in cc.split(",")])
            if bcc:
                recipients.extend([addr.strip() for addr in bcc.split(",")])

            # Verify connection is still active before sending
            # Note: Some servers don't support NOOP, so we'll skip this check
            # The sendmail call itself will fail if connection is lost

            # Send email
            logger.debug(f"Sending email to {recipients}")
            smtp.sendmail(account.email, recipients, msg.as_string())
            logger.debug("Email sent successfully")
            
        except smtplib.SMTPConnectError as e:
            logger.error(f"SMTP connection error: {e}. Server: {smtp_server}:{smtp_port}")
            return False
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication error: {e}. Server: {smtp_server}:{smtp_port}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error sending email: {e}. Server: {smtp_server}:{smtp_port}")
            return False
        except Exception as e:
            logger.error(f"Error during SMTP connection/send: {e}. Server: {smtp_server}:{smtp_port}", exc_info=True)
            return False
        finally:
            # Always close the connection properly
            if smtp:
                try:
                    smtp.quit()
                except Exception:
                    try:
                        smtp.close()
                    except Exception:
                        pass

        logger.info(f"Sent email from {account.email} to {to}: {subject}")

        # Save to Sent folder via IMAP
        try:
            imap = connect_imap(account)
            if imap:
                # Try common sent folder names
                sent_folders = ["INBOX.Sent", "Sent", "Sent Messages", "Sent Items", "[Gmail]/Sent Mail"]
                msg_bytes = msg.as_bytes()

                for folder in sent_folders:
                    try:
                        # Try to select the folder to verify it exists
                        status, _ = imap.select('"' + folder + '"')
                        if status == "OK":
                            # Append message with \Seen flag
                            import time

                            result = imap.append(folder, "\\Seen", imaplib.Time2Internaldate(time.time()), msg_bytes)
                            if result[0] == "OK":
                                logger.info(f"Saved sent email to {folder}")
                                break
                    except Exception:
                        continue

                try:
                    imap.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to save to Sent folder: {e}")

        return True

    except Exception as e:
        logger.error(f"Error sending email: {e}")
        return False


def reply_to_message(
    user_id: int,
    db: Session,
    account_email: str,
    uid: str,
    body: str,
    reply_all: bool = False,
    attachments: List[Tuple[str, bytes, str]] = None,
    folder: str = "INBOX",
) -> bool:
    """Reply to a message."""
    # Get the original message
    original = get_message_by_id(user_id, db, account_email, uid, folder=folder)
    if not original:
        logger.error(f"Original message not found: {account_email}/{uid}")
        return False

    # Get the account
    accounts = get_user_mail_accounts(user_id, db)
    account = next((a for a in accounts if a.email == account_email), None)
    if not account:
        return False

    # Prepare reply
    to = original.sender_email
    subject = original.subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    cc = ""
    if reply_all and original.to:
        # Add all original recipients except ourselves
        cc_addrs = [addr.strip() for addr in original.to.split(",")]
        cc_addrs = [a for a in cc_addrs if account.email.lower() not in a.lower()]
        cc = ", ".join(cc_addrs)

    return send_email(
        account=account, to=to, subject=subject, body=body, attachments=attachments, reply_to_msg=original, cc=cc
    )


def forward_message(
    user_id: int,
    db: Session,
    account_email: str,
    uid: str,
    to: str,
    body: str = "",
    attachments: List[Tuple[str, bytes, str]] = None,
    folder: str = "INBOX",
) -> bool:
    """Forward a message to another recipient."""
    try:
        # Get the original message
        original = get_message_by_id(user_id, db, account_email, uid, folder=folder)
        if not original:
            logger.error(f"Original message not found: {account_email}/{uid} in folder {folder}")
            return False

        # Get the account
        accounts = get_user_mail_accounts(user_id, db)
        account = next((a for a in accounts if a.email == account_email), None)
        if not account:
            logger.error(f"Account not found: {account_email}")
            return False
    except Exception as e:
        logger.error(f"Error preparing forward message: {e}")
        return False

    # Prepare forward subject
    subject = original.subject
    if not subject.lower().startswith("fwd:") and not subject.lower().startswith("fw:"):
        subject = f"Fwd: {subject}"

    # Build forwarded message body
    forward_header = f"""
---------- Forwarded message ----------
From: {original.sender} <{original.sender_email}>
Date: {original.date.strftime("%A, %B %d, %Y at %I:%M %p")}
Subject: {original.subject}
To: {original.to}
"""

    # Get original body
    original_body = original.body_text or ""
    if not original_body.strip() and original.body_html:
        original_body = html_to_text(original.body_html)

    # Combine user's message with forwarded content
    if body.strip():
        full_body = f"{body}\n{forward_header}\n{original_body}"
    else:
        full_body = f"{forward_header}\n{original_body}"

    # Merge original message attachments with any new attachments
    all_attachments = []
    
    # Convert original message attachments to the format expected by send_email
    # Format: List[Tuple[str, bytes, str]] = (filename, data, content_type)
    try:
        for orig_att in original.attachments:
            if orig_att and orig_att.data:
                all_attachments.append((orig_att.filename, orig_att.data, orig_att.content_type))
        logger.debug(f"Forward: found {len(original.attachments)} original attachments, converted {len(all_attachments)}")
    except Exception as e:
        logger.warning(f"Error processing original attachments: {e}")
    
    # Add any new attachments passed in
    if attachments:
        all_attachments.extend(attachments)
    
    logger.debug(f"Forward: total attachments to send: {len(all_attachments)}")
    
    try:
        result = send_email(account=account, to=to, subject=subject, body=full_body, attachments=all_attachments if all_attachments else None)
        if not result:
            logger.error(f"Failed to send forwarded email to {to} (send_email returned False)")
        else:
            logger.info(f"Successfully forwarded message {uid} from {account_email} to {to}")
        return result
    except Exception as e:
        logger.error(f"Error forwarding message to {to}: {e}", exc_info=True)
        return False


def get_attachment(
    user_id: int, db: Session, account_email: str, uid: str, attachment_index: int
) -> Optional[EmailAttachment]:
    """Get a specific attachment from a message."""
    msg = get_message_by_id(user_id, db, account_email, uid)
    if not msg:
        return None

    if attachment_index < 0 or attachment_index >= len(msg.attachments):
        return None

    return msg.attachments[attachment_index]


def format_message_list(
    messages: List[EmailMessage], show_header: bool = True, folder: str = None, account_email: str = None
) -> str:
    """Format messages for display with action buttons."""
    if not messages:
        return "No messages found."

    # Build header based on context
    if show_header:
        if folder and account_email:
            account_short = account_email.split("@")[0]
            lines = [f"## ◈ {folder.upper()} ({account_short}) ◈\n"]
        else:
            lines = ["## ◈ INBOX ◈\n"]
    else:
        lines = []

    for i, msg in enumerate(messages, 1):
        # Unread indicator
        unread = "" if msg.is_read else "**[NEW]** "

        # Truncate subject
        subject = msg.subject[:50] + "..." if len(msg.subject) > 50 else msg.subject

        # Format date
        date_str = msg.date.strftime("%b %d %H:%M")

        # Attachment indicator
        attach = f" [{len(msg.attachments)} files]" if msg.attachments else ""

        # Account indicator (short) - handle folder suffix like "email@example.com (INBOX.Archive)"
        account_part = msg.account.split(" (")[0] if " (" in msg.account else msg.account
        account_short = account_part.split("@")[0]

        # Extract folder from msg.account if present (for search results), otherwise use passed folder
        msg_folder = folder
        if " (" in msg.account and msg.account.endswith(")"):
            msg_folder = msg.account.split(" (")[1].rstrip(")")

        # Build message ID with folder if present (and not INBOX)
        if msg_folder and msg_folder != "INBOX":
            msg_id = f"{msg_folder}:{msg.uid}"
        else:
            msg_id = str(msg.uid)

        lines.append(f"{i}. {unread}**{msg.sender}** - {subject}")
        lines.append(f"   {date_str} | {account_short}{attach}")
        # Action buttons using cmd: prefix links
        read_cmd = f"mail read {account_short} {msg_id}"
        reply_cmd = f"mail reply {account_short} {msg_id} "
        archive_cmd = f"mail archive {account_short} {msg_id}"
        delete_cmd = f"mail delete {account_short} {msg_id}"
        lines.append(
            f"   [Read](cmd:{read_cmd}) | [Reply All](cmd:{reply_cmd}) | [Archive](cmd:{archive_cmd}) | [Delete](cmd:{delete_cmd})"
        )
        lines.append("")

    return "\n".join(lines)


def format_message_detail(msg: EmailMessage, folder: str = "INBOX") -> str:
    """Format a single message for detailed view with action buttons and attachments."""
    account_short = msg.account.split("@")[0]
    # msg_id should include folder if it's not the default INBOX
    msg_id = f"{folder}:{msg.uid}" if folder and folder != "INBOX" else str(msg.uid)

    lines = [
        f"## ◈ {msg.subject} ◈",
        "",
        f"**From:** {msg.sender} <{msg.sender_email}>",
        f"**To:** {msg.to}",
        f"**Date:** {msg.date.strftime('%A, %B %d, %Y at %I:%M %p')}",
        f"**Account:** {msg.account}",
    ]

    if msg.attachments:
        lines.append(f"\n**Attachments:** {len(msg.attachments)} files")
        for i, att in enumerate(msg.attachments):
            size_kb = att.size / 1024
            # TUI uses 'cmd:mail attachment' to trigger the download/open logic in command_service.py
            cmd = f"mail attachment {account_short} {msg_id} {i}"
            lines.append(f"  - [📎 {att.filename}](cmd:{cmd}) ({size_kb:.1f} KB)")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Body content handling
    body_content = ""
    html_has_links = msg.body_html and "<a " in msg.body_html.lower()

    if html_has_links:
        body_content = html_to_text(msg.body_html)
    elif msg.body_text and msg.body_text.strip():
        body_content = msg.body_text
        if any(ind in body_content.lower() for ind in ["<div", "<p>", "<html", "<body"]):
            body_content = html_to_text(body_content)
    elif msg.body_html:
        body_content = html_to_text(msg.body_html)

    if body_content.strip():
        lines.append(body_content)
    else:
        lines.append("(No message body)")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Action buttons - restructured so TUI can easily parse them into actual Button widgets
    reply_cmd = f"mail reply {account_short} {msg_id} "
    forward_cmd = f"mail forward {account_short} {msg_id} "
    summary_cmd = f"mail summary {account_short} {msg_id}"
    archive_cmd = f"mail archive {account_short} {msg_id}"
    delete_cmd = f"mail delete {account_short} {msg_id}"
    translate_cmd = f"mail translate {account_short} {msg_id}"
    extract_event_cmd = f"mail extract-event {account_short} {msg_id}"
    extract_bill_cmd = f"mail extract-bill {account_short} {msg_id}"

    lines.append(f"[Reply](cmd:{reply_cmd}) | [Forward](cmd:{forward_cmd}) | [Summary](cmd:{summary_cmd})")
    lines.append(f"[Archive](cmd:{archive_cmd}) | [Translate](cmd:{translate_cmd}) | [Delete](cmd:{delete_cmd})")
    lines.append(f"[+ Calendar](cmd:{extract_event_cmd}) | [+ Bill](cmd:{extract_bill_cmd})")

    return "\n".join(lines)


# ---- incremental sync + special-use folder discovery (for the Nostr-mailbox Email client) --------

def _account_by_email(user_id: int, db: Session, account_email: str):
    for a in get_user_mail_accounts(user_id, db):
        if a.email == account_email:
            return a
    return None


FOLDER_MAP_KEY = "mail_folders"      # UserSetting: {account_email: {sent/drafts/trash/junk/archive}}
_FOLDER_ROLES = ("sent", "drafts", "trash", "junk", "archive")


def get_folder_map(user_id: int, db: Session) -> dict:
    """The user's manual folder mapping, or {}.

    Lives in its OWN UserSetting rather than inside `mail_accounts` so it survives a password change
    and is not entangled with credentials. Like every other non-exempt UserSetting it is mirrored to
    the relay by users_store.sync_user_kv and restored by hydrate_user_kv, so a rebuilt node comes
    back with the mapping intact.
    """
    import json as _json
    from app.models import UserSetting
    row = db.query(UserSetting).filter(UserSetting.user_id == user_id,
                                       UserSetting.key == FOLDER_MAP_KEY).first()
    if not row or not row.value:
        return {}
    try:
        out = _json.loads(row.value)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def set_folder_map(user_id: int, db: Session, account_email: str, mapping: dict) -> dict:
    """Store the mapping for one account. An empty value for a role means "go back to detecting it"
    rather than "there is no such folder" — otherwise clearing a wrong guess would leave the account
    with no Sent folder at all."""
    import json as _json
    from app.models import UserSetting
    full = get_folder_map(user_id, db)
    clean = {r: str(mapping.get(r) or "").strip() for r in _FOLDER_ROLES}
    clean = {r: v for r, v in clean.items() if v}
    if clean:
        full[account_email] = clean
    else:
        full.pop(account_email, None)
    row = db.query(UserSetting).filter(UserSetting.user_id == user_id,
                                       UserSetting.key == FOLDER_MAP_KEY).first()
    if not row:
        row = UserSetting(user_id=user_id, key=FOLDER_MAP_KEY, value="")
        db.add(row)
    row.value = _json.dumps(full, separators=(",", ":"))
    db.commit()
    return full


def list_special_folders(user_id: int, db: Session, account_email: str) -> dict:
    """Resolve an account's real folders + special-use mailboxes (RFC 6154 \\Sent etc., with name
    heuristics as a fallback). Returns {'all':[names], 'sent':name|None, 'drafts':.., 'trash':..,
    'junk':.., 'archive':..} — so the GUI maps 'Sent' to the server's ACTUAL sent folder."""
    out = {"all": [], "sent": None, "drafts": None, "trash": None, "junk": None, "archive": None}
    acc = _account_by_email(user_id, db, account_email)
    if not acc:
        return out
    imap = connect_imap(acc)
    if not imap:
        return out
    try:
        res = run_with_timeout(lambda: imap.list(), timeout=6)
        if not res or res[0] != "OK":
            return out
        for line in (res[1] or []):
            try:
                dec = line.decode() if isinstance(line, bytes) else line
            except Exception:
                continue
            fm = re.search(r'^\(([^)]*)\)', dec)
            flags = (fm.group(1).lower() if fm else "")
            nm = re.search(r'"([^"]+)"\s*$|(\S+)\s*$', dec)
            name = ((nm.group(1) or nm.group(2)) if nm else None)
            if not name or name.lower() in ("[gmail]", "[google mail]"):
                continue
            out["all"].append(name)
            for su, key in (("\\sent", "sent"), ("\\drafts", "drafts"), ("\\trash", "trash"),
                            ("\\junk", "junk"), ("\\archive", "archive")):
                if su in flags and not out[key]:
                    out[key] = name
        # THE USER'S OWN MAPPING WINS. RFC 6154 special-use flags plus name heuristics get this
        # right on most servers and wrong on some — and when Sent is wrong, a message you just sent
        # is filed somewhere this app never looks, which reads as "it never sent". An explicit
        # choice in Settings → Mail overrides the guess; a role left blank keeps being detected.
        try:
            override = (get_folder_map(user_id, db) or {}).get(account_email) or {}
            for key in _FOLDER_ROLES:
                v = str(override.get(key) or "").strip()
                if v:
                    out[key] = v
                    if v not in out["all"]:
                        out["all"].append(v)
        except Exception as e:
            logger.debug("[mail] folder override unreadable: %s", e)

        def _heur(*subs):
            for n in out["all"]:
                base = n.lower().replace("[gmail]/", "").split("/")[-1].split(".")[-1]
                if any(s in base for s in subs):
                    return n
            return None
        out["sent"] = out["sent"] or _heur("sent")
        out["drafts"] = out["drafts"] or _heur("draft")
        out["trash"] = out["trash"] or _heur("trash", "deleted")
        out["junk"] = out["junk"] or _heur("junk", "spam")
        out["archive"] = out["archive"] or _heur("archive", "all mail")
        out["all"] = sorted(set(out["all"]))
        return out
    except Exception as e:
        logger.warning(f"list_special_folders failed for {account_email}: {e}")
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def list_uids(account: MailAccount, folder: str = "INBOX") -> List[str]:
    """Cheap UID SEARCH ALL → every UID in a folder (oldest→newest). Used to diff against what's
    already mirrored so we FETCH only genuinely-new messages (efficient full sync)."""
    imap = connect_imap(account)
    if not imap:
        return []
    try:
        sel = run_with_timeout(lambda: imap.select(f'"{folder}"', readonly=True), timeout=6)
        if not sel or sel[0] != "OK":
            return []
        res = run_with_timeout(lambda: imap.uid("SEARCH", None, "ALL"), timeout=10)
        if not res or res[0] != "OK" or not res[1] or not res[1][0]:
            return []
        return [u.decode() if isinstance(u, bytes) else u for u in res[1][0].split()]
    except Exception as e:
        logger.warning(f"list_uids {account.email}/{folder} failed: {e}")
        return []
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def fetch_by_uids(account: MailAccount, folder: str, uids: List[str]) -> List[EmailMessage]:
    """FETCH specific UIDs (in batches) and parse them — pairs with list_uids for incremental sync."""
    if not uids:
        return []
    imap = connect_imap(account)
    if not imap:
        return []
    out: List[EmailMessage] = []
    try:
        sel = run_with_timeout(lambda: imap.select(f'"{folder}"', readonly=True), timeout=6)
        if not sel or sel[0] != "OK":
            return []
        CHUNK = 40
        for i in range(0, len(uids), CHUNK):
            uidset = ",".join(uids[i:i + CHUNK])
            try:
                res = run_with_timeout(lambda: imap.uid("FETCH", uidset, "(FLAGS RFC822)"), timeout=45)
            except Exception as e:
                logger.debug(f"fetch_by_uids batch failed: {e}")
                continue
            if not res or res[0] != "OK" or not res[1]:
                continue
            for part in res[1]:
                if not isinstance(part, tuple) or len(part) < 2 or not part[1]:
                    continue
                head = part[0].decode(errors="ignore") if isinstance(part[0], bytes) else str(part[0])
                um = re.search(r"UID (\d+)", head)
                _fm = re.search(r"FLAGS \(([^)]*)\)", head)   # real read/unread (parse_email otherwise defaults read=True)
                uid = um.group(1) if um else None
                if not uid:
                    continue
                msg = parse_email(part[1], uid, account.email)
                if msg:
                    msg.is_read = bool(_fm and "\\Seen" in _fm.group(1))
                    out.append(msg)
        return out
    except Exception as e:
        logger.warning(f"fetch_by_uids {account.email}/{folder} failed: {e}")
        return []
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def move_message(user_id: int, db: Session, account_email: str, uid: str, src_folder: str, dest_folder: str) -> bool:
    """Move a message src→dest (COPY + \\Deleted + EXPUNGE on the source). Used to send a 'deleted'
    message to Trash instead of expunging it permanently. Returns False if it can't (caller falls back)."""
    if not dest_folder or dest_folder == src_folder:
        return False
    acc = _account_by_email(user_id, db, account_email)
    if not acc:
        return False
    imap = connect_imap(acc)
    if not imap:
        return False
    try:
        sel = run_with_timeout(lambda: imap.select('"' + src_folder + '"'), timeout=6)
        if not sel or sel[0] != "OK":
            return False
        r = run_with_timeout(lambda: imap.uid("COPY", uid, '"' + dest_folder + '"'), timeout=10)
        if not r or r[0] != "OK":
            return False
        run_with_timeout(lambda: imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)"), timeout=8)
        run_with_timeout(lambda: imap.expunge(), timeout=10)
        return True
    except Exception as e:
        logger.warning(f"move_message {account_email} {src_folder}->{dest_folder} failed: {e}")
        return False
    finally:
        try:
            imap.logout()
        except Exception:
            pass
