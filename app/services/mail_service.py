"""
Mail Service - IMAP/SMTP email integration.

Provides:
- IMAP inbox checking across multiple accounts
- Email viewing with attachments
- Reply/Reply-all functionality
- Send new emails with attachments
- Delete messages
"""
import logging
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from email.utils import parseaddr, formataddr, formatdate
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
import base64
import re
import json

from sqlalchemy.orm import Session
from app.models import UserSetting

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
    """Get user's configured mail accounts."""
    setting = db.query(UserSetting).filter(
        UserSetting.user_id == user_id,
        UserSetting.key == "mail_accounts"
    ).first()

    if not setting or not setting.value:
        return []

    try:
        accounts_data = json.loads(setting.value)
        return [MailAccount(**acc) for acc in accounts_data]
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Invalid mail_accounts JSON for user {user_id}: {e}")
        return []


def save_user_mail_accounts(user_id: int, accounts: List[MailAccount], db: Session):
    """Save user's mail accounts."""
    setting = db.query(UserSetting).filter(
        UserSetting.user_id == user_id,
        UserSetting.key == "mail_accounts"
    ).first()

    accounts_data = [
        {
            "email": acc.email,
            "password": acc.password,
            "imap_server": acc.imap_server,
            "imap_port": acc.imap_port,
            "smtp_server": acc.smtp_server,
            "smtp_port": acc.smtp_port,
            "use_ssl": acc.use_ssl
        }
        for acc in accounts
    ]

    if setting:
        setting.value = json.dumps(accounts_data)
    else:
        setting = UserSetting(
            user_id=user_id,
            key="mail_accounts",
            value=json.dumps(accounts_data)
        )
        db.add(setting)

    db.commit()


def decode_mime_header(header_value: str) -> str:
    """Decode a MIME-encoded header value."""
    if not header_value:
        return ""

    decoded_parts = []
    for part, encoding in decode_header(header_value):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
            except (LookupError, UnicodeDecodeError):
                decoded_parts.append(part.decode('utf-8', errors='replace'))
        else:
            decoded_parts.append(part)

    return ''.join(decoded_parts)


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
                    charset = part.get_content_charset() or 'utf-8'
                    text_body = part.get_payload(decode=True).decode(charset, errors='replace')
                except Exception:
                    text_body = str(part.get_payload())

            elif content_type == "text/html":
                try:
                    charset = part.get_content_charset() or 'utf-8'
                    html_body = part.get_payload(decode=True).decode(charset, errors='replace')
                except Exception:
                    html_body = str(part.get_payload())
    else:
        content_type = msg.get_content_type()
        try:
            charset = msg.get_content_charset() or 'utf-8'
            payload = msg.get_payload(decode=True)
            if payload:
                decoded = payload.decode(charset, errors='replace')
                if content_type == "text/html":
                    html_body = decoded
                else:
                    text_body = decoded
        except Exception:
            text_body = str(msg.get_payload())

    return text_body, html_body


def get_attachments(msg: email.message.Message) -> List[EmailAttachment]:
    """Extract attachments from email message."""
    attachments = []

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
                    attachments.append(EmailAttachment(
                        filename=filename,
                        content_type=part.get_content_type(),
                        size=len(data),
                        data=data
                    ))
                except Exception as e:
                    logger.debug(f"Error extracting attachment {filename}: {e}")

    return attachments


def connect_imap(account: MailAccount) -> Optional[imaplib.IMAP4_SSL]:
    """Connect to IMAP server."""
    try:
        if account.use_ssl:
            imap = imaplib.IMAP4_SSL(account.imap_server, account.imap_port)
        else:
            imap = imaplib.IMAP4(account.imap_server, account.imap_port)

        imap.login(account.email, account.password)
        return imap
    except Exception as e:
        logger.error(f"IMAP connection failed for {account.email}: {e}")
        return None


def fetch_messages(
    account: MailAccount,
    folder: str = "INBOX",
    limit: int = 20,
    unread_only: bool = False
) -> List[EmailMessage]:
    """Fetch messages from an IMAP account."""
    messages = []

    imap = connect_imap(account)
    if not imap:
        return messages

    try:
        imap.select(folder)

        # Search criteria
        if unread_only:
            status, data = imap.search(None, "UNSEEN")
        else:
            status, data = imap.search(None, "ALL")

        if status != "OK":
            return messages

        # Get message UIDs (newest first)
        uids = data[0].split()
        uids = uids[-limit:] if len(uids) > limit else uids
        uids.reverse()  # Newest first

        for uid in uids:
            try:
                status, msg_data = imap.fetch(uid, "(RFC822 FLAGS)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Parse flags
                flags_data = msg_data[0][0].decode() if msg_data[0][0] else ""
                is_read = "\\Seen" in flags_data

                # Parse date
                date_str = msg.get("Date", "")
                try:
                    from email.utils import parsedate_to_datetime
                    msg_date = parsedate_to_datetime(date_str)
                except Exception:
                    msg_date = datetime.now()

                # Parse sender
                sender_raw = msg.get("From", "")
                sender_name, sender_email = parseaddr(sender_raw)
                sender_name = decode_mime_header(sender_name) or sender_email

                # Get body and attachments
                text_body, html_body = get_email_body(msg)
                attachments = get_attachments(msg)

                messages.append(EmailMessage(
                    uid=uid.decode(),
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
                    in_reply_to=msg.get("In-Reply-To", "")
                ))

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
    user_id: int,
    db: Session,
    limit_per_account: int = 10,
    unread_only: bool = False
) -> List[EmailMessage]:
    """Fetch messages from all user's accounts."""
    accounts = get_user_mail_accounts(user_id, db)
    all_messages = []

    for account in accounts:
        messages = fetch_messages(account, limit=limit_per_account, unread_only=unread_only)
        all_messages.extend(messages)

    # Sort by date (newest first)
    all_messages.sort(key=lambda m: m.date, reverse=True)
    return all_messages


def search_messages(
    user_id: int,
    db: Session,
    account_email: str,
    query: str,
    limit: int = 20
) -> List[EmailMessage]:
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
                    return []

                folders = []
                for folder_line in folder_data:
                    # Parse folder name from IMAP LIST response
                    # Format: (\\flags) "delimiter" "folder_name"
                    try:
                        decoded = folder_line.decode() if isinstance(folder_line, bytes) else folder_line
                        parts = decoded.split('"')
                        if len(parts) >= 4:
                            folder_name = parts[-2]
                            # Skip special folders that can't be searched
                            if folder_name.lower() not in ('[gmail]', '[google mail]'):
                                folders.append(folder_name)
                    except Exception:
                        continue

                # Search each folder - search FROM, TO, SUBJECT, and BODY
                logger.info(f"Searching {len(folders)} folders for '{query}'")

                for folder in folders:
                    try:
                        status, _ = imap.select(folder, readonly=True)
                        if status != "OK":
                            logger.debug(f"Could not select folder: {folder}")
                            continue

                        # Search multiple criteria and combine results
                        all_uids = set()
                        for criteria in [f'FROM "{query}"', f'TO "{query}"', f'SUBJECT "{query}"']:
                            try:
                                status, data = imap.search(None, criteria)
                                if status == "OK" and data[0]:
                                    all_uids.update(data[0].split())
                            except Exception:
                                pass

                        logger.debug(f"Search in {folder}: {len(all_uids)} results")
                        if not all_uids:
                            continue

                        uids = list(all_uids)
                        # Get most recent matches first
                        for uid in reversed(uids[-limit:]):
                            if len(messages) >= limit:
                                break
                            status, msg_data = imap.fetch(uid, "(RFC822)")
                            if status != "OK" or not msg_data[0]:
                                continue

                            raw_email = msg_data[0][1]
                            msg = parse_email(raw_email, uid.decode(), account.email)
                            if msg:
                                # Add folder info to message
                                msg.account = f"{account.email} ({folder})"
                                messages.append(msg)

                    except Exception as e:
                        logger.debug(f"Error searching folder {folder}: {e}")
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
    user_id: int,
    db: Session,
    account_email: str,
    uid: str
) -> Optional[EmailMessage]:
    """Get a specific message by account and UID."""
    accounts = get_user_mail_accounts(user_id, db)

    for account in accounts:
        if account.email == account_email:
            messages = fetch_messages(account, limit=100)
            for msg in messages:
                if msg.uid == uid:
                    return msg

    return None


def delete_message(
    user_id: int,
    db: Session,
    account_email: str,
    uid: str
) -> bool:
    """Delete a message by UID."""
    accounts = get_user_mail_accounts(user_id, db)

    for account in accounts:
        if account.email == account_email:
            imap = connect_imap(account)
            if not imap:
                return False

            try:
                imap.select("INBOX")
                imap.store(uid.encode(), '+FLAGS', '\\Deleted')
                imap.expunge()
                logger.info(f"Deleted message {uid} from {account_email}")
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


def delete_all_messages(
    user_id: int,
    db: Session,
    account_email: str
) -> int:
    """Delete ALL messages in the INBOX for the specified account. Returns count, or -1 on error."""
    accounts = get_user_mail_accounts(user_id, db)

    for account in accounts:
        if account.email == account_email:
            imap = connect_imap(account)
            if not imap:
                return -1

            try:
                imap.select("INBOX")
                # Search for all messages in this inbox
                status, data = imap.search(None, "ALL")
                if status != "OK":
                    return -1

                message_ids = data[0].split()
                if not message_ids:
                    return 0

                count = len(message_ids)

                # Mark all for deletion
                for uid in message_ids:
                    imap.store(uid, '+FLAGS', '\\Deleted')

                # Expunge to permanently delete
                imap.expunge()
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


def archive_message(
    user_id: int,
    db: Session,
    account_email: str,
    uid: str
) -> bool:
    """Archive a message by moving to INBOX.Archive folder."""
    accounts = get_user_mail_accounts(user_id, db)

    for account in accounts:
        if account.email == account_email:
            imap = connect_imap(account)
            if not imap:
                return False

            try:
                imap.select("INBOX")

                # Use INBOX.Archive as the standard archive folder
                archive_folder = "INBOX.Archive"

                # Check if it exists, create if not
                status, folders = imap.list()
                folder_exists = False
                if status == "OK":
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

                # Copy to archive folder
                result = imap.copy(uid.encode(), archive_folder)
                if result[0] != "OK":
                    logger.error(f"Failed to copy message to {archive_folder}")
                    return False

                # Delete from inbox
                imap.store(uid.encode(), '+FLAGS', '\\Deleted')
                imap.expunge()

                logger.info(f"Archived message {uid} from {account_email} to {archive_folder}")
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
    bcc: str = ""
) -> bool:
    """Send an email via SMTP."""
    try:
        # Create message
        if attachments or html_body:
            msg = MIMEMultipart('mixed')

            # Add body
            if html_body:
                body_part = MIMEMultipart('alternative')
                body_part.attach(MIMEText(body, 'plain', 'utf-8'))
                body_part.attach(MIMEText(html_body, 'html', 'utf-8'))
                msg.attach(body_part)
            else:
                msg.attach(MIMEText(body, 'plain', 'utf-8'))

            # Add attachments
            if attachments:
                for filename, data, content_type in attachments:
                    part = MIMEBase(*content_type.split('/', 1))
                    part.set_payload(data)
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                    msg.attach(part)
        else:
            msg = MIMEText(body, 'plain', 'utf-8')

        # Set headers
        msg['From'] = account.email
        msg['To'] = to
        msg['Subject'] = subject
        msg['Date'] = formatdate(localtime=True)

        if cc:
            msg['Cc'] = cc
        if bcc:
            msg['Bcc'] = bcc

        # Threading headers for replies
        if reply_to_msg:
            if reply_to_msg.message_id:
                msg['In-Reply-To'] = reply_to_msg.message_id
                refs = reply_to_msg.references
                if refs:
                    msg['References'] = f"{refs} {reply_to_msg.message_id}"
                else:
                    msg['References'] = reply_to_msg.message_id

        # Connect and send
        smtp_server = account.smtp_server or account.imap_server
        smtp_port = account.smtp_port or 587

        if smtp_port == 465:
            smtp = smtplib.SMTP_SSL(smtp_server, smtp_port)
        else:
            smtp = smtplib.SMTP(smtp_server, smtp_port)
            smtp.starttls()

        smtp.login(account.email, account.password)

        # Collect all recipients
        recipients = [to]
        if cc:
            recipients.extend([addr.strip() for addr in cc.split(',')])
        if bcc:
            recipients.extend([addr.strip() for addr in bcc.split(',')])

        smtp.sendmail(account.email, recipients, msg.as_string())
        smtp.quit()

        logger.info(f"Sent email from {account.email} to {to}: {subject}")
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
    attachments: List[Tuple[str, bytes, str]] = None
) -> bool:
    """Reply to a message."""
    # Get the original message
    original = get_message_by_id(user_id, db, account_email, uid)
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
        cc_addrs = [addr.strip() for addr in original.to.split(',')]
        cc_addrs = [a for a in cc_addrs if account.email.lower() not in a.lower()]
        cc = ', '.join(cc_addrs)

    return send_email(
        account=account,
        to=to,
        subject=subject,
        body=body,
        attachments=attachments,
        reply_to_msg=original,
        cc=cc
    )


def get_attachment(
    user_id: int,
    db: Session,
    account_email: str,
    uid: str,
    attachment_index: int
) -> Optional[EmailAttachment]:
    """Get a specific attachment from a message."""
    msg = get_message_by_id(user_id, db, account_email, uid)
    if not msg:
        return None

    if attachment_index < 0 or attachment_index >= len(msg.attachments):
        return None

    return msg.attachments[attachment_index]


def format_message_list(messages: List[EmailMessage]) -> str:
    """Format messages for display."""
    if not messages:
        return "No messages found."

    lines = ["## Inbox\n"]

    for i, msg in enumerate(messages, 1):
        # Unread indicator
        unread = "" if msg.is_read else "**[NEW]** "

        # Truncate subject
        subject = msg.subject[:50] + "..." if len(msg.subject) > 50 else msg.subject

        # Format date
        date_str = msg.date.strftime("%b %d %H:%M")

        # Attachment indicator
        attach = f" [{len(msg.attachments)} files]" if msg.attachments else ""

        # Account indicator (short)
        account_short = msg.account.split('@')[0]

        lines.append(f"{i}. {unread}**{msg.sender}** - {subject}")
        lines.append(f"   {date_str} | {account_short}{attach}")
        lines.append(f"   `mail read {account_short} {msg.uid}`")
        lines.append("")

    return "\n".join(lines)


def format_message_detail(msg: EmailMessage) -> str:
    """Format a single message for detailed view."""
    lines = [
        f"## {msg.subject}",
        "",
        f"**From:** {msg.sender} <{msg.sender_email}>",
        f"**To:** {msg.to}",
        f"**Date:** {msg.date.strftime('%A, %B %d, %Y at %I:%M %p')}",
        f"**Account:** {msg.account}",
    ]

    if msg.attachments:
        lines.append(f"**Attachments:** {len(msg.attachments)} files")
        account_short = msg.account.split('@')[0]
        for i, att in enumerate(msg.attachments):
            size_kb = att.size / 1024
            # Create download link
            download_url = f"/api/mail/attachment/{account_short}/{msg.uid}/{i}"
            lines.append(f"  - [{att.filename}]({download_url}) ({size_kb:.1f} KB)")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Body (prefer text, fall back to stripped HTML)
    if msg.body_text:
        lines.append(msg.body_text)
    elif msg.body_html:
        # Simple HTML stripping
        import re
        text = re.sub(r'<[^>]+>', '', msg.body_html)
        text = re.sub(r'\s+', ' ', text).strip()
        lines.append(text)
    else:
        lines.append("(No message body)")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Reply commands
    account_short = msg.account.split('@')[0]
    lines.append(f"**Reply:** `mail reply {account_short} {msg.uid} <message>`")
    lines.append(f"**Delete:** `mail delete {account_short} {msg.uid}`")

    return "\n".join(lines)
