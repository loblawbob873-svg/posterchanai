"""
Email Service for SMTP sending and IMAP sent folder storage.
"""
import smtplib
import imaplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid
from typing import Optional, Tuple
from datetime import datetime
from sqlalchemy.orm import Session

from app.models import Setting


class EmailService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        """Load email settings from database"""
        settings = {s.key: s.value for s in self.db.query(Setting).all()}

        # SMTP settings
        self.smtp_enabled = settings.get("smtp_enabled", "false").lower() == "true"
        self.smtp_host = settings.get("smtp_host", "")
        self.smtp_port = int(settings.get("smtp_port", "587"))
        self.smtp_username = settings.get("smtp_username", "")
        self.smtp_password = settings.get("smtp_password", "")
        self.smtp_from_email = settings.get("smtp_from_email", "")
        self.smtp_from_name = settings.get("smtp_from_name", "Posterchanai")
        self.smtp_use_tls = settings.get("smtp_use_tls", "true").lower() == "true"
        self.smtp_use_ssl = settings.get("smtp_use_ssl", "false").lower() == "true"

        # IMAP settings
        self.imap_enabled = settings.get("imap_enabled", "false").lower() == "true"
        self.imap_host = settings.get("imap_host", "")
        self.imap_port = int(settings.get("imap_port", "993"))
        self.imap_username = settings.get("imap_username", "")
        self.imap_password = settings.get("imap_password", "")
        self.imap_use_ssl = settings.get("imap_use_ssl", "true").lower() == "true"
        self.imap_sent_folder = settings.get("imap_sent_folder", "Sent")

    def _create_message(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None
    ) -> MIMEMultipart:
        """Create an email message"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{self.smtp_from_name} <{self.smtp_from_email}>"
        msg["To"] = to_email
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()

        # Add plain text body
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # Add HTML body if provided
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))

        return msg

    def _save_to_sent_folder(self, msg: MIMEMultipart) -> Tuple[bool, str]:
        """Save email to IMAP Sent folder"""
        if not self.imap_enabled:
            return True, "IMAP disabled, skipping sent folder"

        if not self.imap_host:
            return False, "IMAP host not configured"

        try:
            # Connect to IMAP
            if self.imap_use_ssl:
                context = ssl.create_default_context()
                imap = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=context)
            else:
                imap = imaplib.IMAP4(self.imap_host, self.imap_port)

            # Login
            imap.login(self.imap_username or self.smtp_username, self.imap_password or self.smtp_password)

            # Append to sent folder
            date_time = imaplib.Time2Internaldate(datetime.now().timestamp())
            result = imap.append(
                self.imap_sent_folder,
                "\\Seen",
                date_time,
                msg.as_bytes()
            )

            imap.logout()

            if result[0] == "OK":
                return True, "Email saved to sent folder"
            else:
                return False, f"Failed to save to sent folder: {result}"

        except Exception as e:
            return False, f"IMAP error: {str(e)}"

    def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        save_to_sent: bool = True
    ) -> Tuple[bool, str]:
        """
        Send an email via SMTP.

        Returns:
            Tuple of (success: bool, message: str)
        """
        if not self.smtp_enabled:
            return False, "SMTP is not enabled"

        if not self.smtp_host:
            return False, "SMTP host not configured"

        if not self.smtp_from_email:
            return False, "From email not configured"

        try:
            # Create message
            msg = self._create_message(to_email, subject, body, html_body)

            # Connect to SMTP server
            if self.smtp_use_ssl:
                # Direct SSL connection (port 465)
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context)
            else:
                # Regular connection, optionally with STARTTLS
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                if self.smtp_use_tls:
                    context = ssl.create_default_context()
                    server.starttls(context=context)

            # Authenticate if credentials provided
            if self.smtp_username and self.smtp_password:
                server.login(self.smtp_username, self.smtp_password)

            # Send email
            server.sendmail(self.smtp_from_email, to_email, msg.as_string())
            server.quit()

            # Save to sent folder if enabled
            if save_to_sent and self.imap_enabled:
                imap_success, imap_msg = self._save_to_sent_folder(msg)
                if not imap_success:
                    return True, f"Email sent, but failed to save to sent folder: {imap_msg}"

            return True, "Email sent successfully"

        except smtplib.SMTPAuthenticationError as e:
            return False, f"SMTP authentication failed: {str(e)}"
        except smtplib.SMTPConnectError as e:
            return False, f"Failed to connect to SMTP server: {str(e)}"
        except smtplib.SMTPException as e:
            return False, f"SMTP error: {str(e)}"
        except Exception as e:
            return False, f"Error sending email: {str(e)}"

    def send_test_email(self, to_email: str) -> Tuple[bool, str]:
        """Send a test email to verify configuration"""
        subject = "Posterchanai Test Email"
        body = """This is a test email from Posterchanai.

If you received this email, your SMTP configuration is working correctly.

Configuration details:
- SMTP Host: {smtp_host}
- SMTP Port: {smtp_port}
- TLS: {smtp_tls}
- SSL: {smtp_ssl}
- From: {from_email}

Sent at: {timestamp}
""".format(
            smtp_host=self.smtp_host,
            smtp_port=self.smtp_port,
            smtp_tls="Enabled" if self.smtp_use_tls else "Disabled",
            smtp_ssl="Enabled" if self.smtp_use_ssl else "Disabled",
            from_email=self.smtp_from_email,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        html_body = """
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; background: #1a1a2e; color: #fff; padding: 20px; }}
        .container {{ max-width: 600px; margin: 0 auto; background: #16213e; border-radius: 12px; padding: 30px; }}
        h1 {{ color: #4a9eff; margin-bottom: 20px; }}
        .success {{ color: #27ae60; font-size: 18px; margin-bottom: 20px; }}
        .details {{ background: #0f3460; padding: 20px; border-radius: 8px; margin: 20px 0; }}
        .detail-row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #1a1a2e; }}
        .label {{ color: #888; }}
        .value {{ color: #fff; }}
        .footer {{ color: #666; font-size: 12px; margin-top: 30px; text-align: center; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Posterchanai Test Email</h1>
        <p class="success">Your SMTP configuration is working correctly!</p>
        <div class="details">
            <div class="detail-row">
                <span class="label">SMTP Host:</span>
                <span class="value">{smtp_host}</span>
            </div>
            <div class="detail-row">
                <span class="label">SMTP Port:</span>
                <span class="value">{smtp_port}</span>
            </div>
            <div class="detail-row">
                <span class="label">TLS:</span>
                <span class="value">{smtp_tls}</span>
            </div>
            <div class="detail-row">
                <span class="label">SSL:</span>
                <span class="value">{smtp_ssl}</span>
            </div>
            <div class="detail-row">
                <span class="label">From:</span>
                <span class="value">{from_email}</span>
            </div>
        </div>
        <p class="footer">Sent at: {timestamp}</p>
    </div>
</body>
</html>
""".format(
            smtp_host=self.smtp_host,
            smtp_port=self.smtp_port,
            smtp_tls="Enabled" if self.smtp_use_tls else "Disabled",
            smtp_ssl="Enabled" if self.smtp_use_ssl else "Disabled",
            from_email=self.smtp_from_email,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        return self.send_email(to_email, subject, body, html_body)


    def send_verification_email(self, to_email: str, username: str, verify_url: str) -> Tuple[bool, str]:
        """Send email verification link"""
        subject = "Verify your Posterchanai account"
        body = f"""Hello {username},

Thank you for registering with Posterchanai!

Please click the link below to verify your email address:
{verify_url}

This link will expire in 24 hours.

If you did not create an account, you can ignore this email.

Best regards,
Posterchanai
"""

        html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; background: #1a1a2e; color: #fff; padding: 20px; }}
        .container {{ max-width: 600px; margin: 0 auto; background: #16213e; border-radius: 12px; padding: 30px; }}
        h1 {{ color: #4a9eff; margin-bottom: 20px; }}
        .message {{ color: #ccc; line-height: 1.6; margin-bottom: 20px; }}
        .button {{ display: inline-block; background: #4a9eff; color: #fff; padding: 15px 30px; text-decoration: none; border-radius: 8px; font-weight: bold; margin: 20px 0; }}
        .button:hover {{ background: #3a8eef; }}
        .link {{ color: #4a9eff; word-break: break-all; }}
        .footer {{ color: #666; font-size: 12px; margin-top: 30px; text-align: center; }}
        .expire {{ color: #e74c3c; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Welcome to Posterchanai!</h1>
        <p class="message">Hello {username},</p>
        <p class="message">Thank you for registering! Please verify your email address by clicking the button below:</p>
        <p style="text-align: center;">
            <a href="{verify_url}" class="button">Verify Email Address</a>
        </p>
        <p class="message">Or copy and paste this link into your browser:</p>
        <p class="link">{verify_url}</p>
        <p class="expire">This link will expire in 24 hours.</p>
        <p class="footer">If you did not create an account, you can safely ignore this email.</p>
    </div>
</body>
</html>
"""

        return self.send_email(to_email, subject, body, html_body, save_to_sent=True)


def get_email_service(db: Session) -> EmailService:
    return EmailService(db)
