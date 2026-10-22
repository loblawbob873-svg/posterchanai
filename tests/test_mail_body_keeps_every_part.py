"""An email's body is every inline part, in order -- never just the last one (2026-10-22: "the preview says 'I
received your email, the pdf is cleaner text a…' but that never appears when you open the email"; the opened
message started at "Sincerely, Sam Bole").

Apple Mail (and others) send a reply with a PDF in the middle as SEVERAL text/html parts: the reply, the PDF, the
signature. get_email_body assigned each text/html part over the last, so only the signature survived -- while the
preview, built from the text/plain part, showed the reply. A forwarded email (message/rfc822) is a separate message
and is never merged into the body; an attachment is never the body.
"""
from email.mime.application import MIMEApplication
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.services.mail_service import get_email_body, parse_email


def _apple_split():
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("I received your email, the pdf is cleaner text.\n\nSincerely,\nSam", "plain"))
    mixed = MIMEMultipart("mixed")
    mixed.attach(MIMEText("<html><body><p>I received your email, the pdf is cleaner text.</p></body></html>", "html"))
    pdf = MIMEApplication(b"%PDF-1.4 fake", _subtype="pdf")
    pdf.add_header("Content-Disposition", "inline", filename="proof.pdf")
    mixed.attach(pdf)
    mixed.attach(MIMEText("<html><body><p>Sincerely,<br>Sam</p></body></html>", "html"))
    alt.attach(mixed)
    return alt


def test_a_reply_split_around_a_pdf_keeps_the_reply_and_the_signature_in_order():
    text, html = get_email_body(_apple_split())
    assert "I received your email" in html, "the reply above the PDF was dropped"
    assert "Sincerely" in html
    assert html.index("I received your email") < html.index("Sincerely"), "the pieces are out of order"
    assert text.startswith("I received your email")


def test_an_ordinary_text_and_html_email_is_unchanged():
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("hello there", "plain"))
    alt.attach(MIMEText("<p>hello there</p>", "html"))
    assert get_email_body(alt) == ("hello there", "<p>hello there</p>")


def test_a_forwarded_email_is_not_merged_into_the_body():
    outer = MIMEMultipart("mixed")
    outer.attach(MIMEText("<p>see below, FYI</p>", "html"))
    inner = MIMEMultipart("alternative")
    inner.attach(MIMEText("the forwarded text", "plain"))
    inner.attach(MIMEText("<p>the forwarded html</p>", "html"))
    outer.attach(MIMEMessage(inner))
    text, html = get_email_body(outer)
    assert "see below" in html and "forwarded html" not in html, html
    assert "the forwarded text" not in text


def test_an_attached_html_file_is_never_the_body():
    outer = MIMEMultipart("mixed")
    outer.attach(MIMEText("the real body", "plain"))
    att = MIMEText("<p>an attached page</p>", "html")
    att.add_header("Content-Disposition", "attachment", filename="page.html")
    outer.attach(att)
    text, html = get_email_body(outer)
    assert text == "the real body" and not html


def test_a_single_part_email_is_unchanged():
    assert get_email_body(MIMEText("just text", "plain")) == ("just text", None)
    assert get_email_body(MIMEText("<b>just html</b>", "html")) == ("", "<b>just html</b>")


def test_the_whole_parse_keeps_the_reply():
    msg = _apple_split()
    msg["From"] = "Sam <sam@example.com>"
    msg["Subject"] = "Re: proof"
    parsed = parse_email(msg.as_bytes(), "7510", "me@example.com")
    assert "I received your email" in (parsed.body_html or "")


# ---- the repair of bodies stored by the old parser -------------------------------------------------------------
from app.services import mail_sync


def test_a_body_that_lost_its_head_is_recognised_and_a_whole_one_is_not():
    cut = {"body_text": "I received your email, the pdf is cleaner text now.\n\nSincerely,\nSam",
           "body_html": "<p>Sincerely,<br>Sam</p>", "attachments": [{"filename": "proof.pdf"}]}
    assert mail_sync.body_lost_its_head(cut)
    whole = {**cut, "body_html": "<p>I received your email, the pdf is cleaner text now.</p><p>Sincerely,<br>Sam</p>"}
    assert not mail_sync.body_lost_its_head(whole)
    assert not mail_sync.body_lost_its_head({**cut, "attachments": []}), "only the split-around-an-attachment shape"
    assert not mail_sync.body_lost_its_head({**cut, "body_html": ""}), "a text-only message is not cut"


def test_the_repair_refetches_stores_the_whole_body_and_returns_it(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    cut = {"uid": "7510", "body_text": "I received your email, the pdf is cleaner text now.\n\nSincerely,\nSam",
           "body_html": "<p>Sincerely,<br>Sam</p>", "attachments": [{"filename": "proof.pdf"}], "flags": {"read": True}}
    msg = _apple_split()
    whole = parse_email(msg.as_bytes(), "7510", "me@example.com")
    stored = {}
    monkeypatch.setattr(mail_sync.mail_service, "fetch_by_uids", lambda acc, folder, uids: [whole])
    monkeypatch.setattr(mail_sync.nostr_store, "user_storage_seckey", lambda db, user: b"\x01" * 32)
    monkeypatch.setattr(mail_sync.nostr_service, "derive_pubkey", lambda sk: "a" * 64)

    async def offload(db, pk, doc):
        return doc

    async def store(sk, email, folder, doc):
        stored.update(doc); return True
    monkeypatch.setattr(mail_sync, "offload_body", offload)
    monkeypatch.setattr(mail_sync.mail_store, "store_message", store)
    out = asyncio.run(mail_sync.repair_split_body(None, SimpleNamespace(id=1), SimpleNamespace(email="me@example.com"), "INBOX", cut))
    assert "I received your email" in out["body_html"] and "I received your email" in stored["body_html"]
    assert stored["flags"] == {"read": True} and stored["uid"] == "7510", "the repair dropped the message's state"


def test_a_failed_refetch_leaves_the_message_as_it_was(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    cut = {"uid": "1", "body_text": "Opening words of a reply here.\n\nThanks", "body_html": "<p>Thanks</p>", "attachments": [{}]}

    def boom(*a):
        raise OSError("imap down")
    monkeypatch.setattr(mail_sync.mail_service, "fetch_by_uids", boom)
    assert asyncio.run(mail_sync.repair_split_body(None, SimpleNamespace(id=1), SimpleNamespace(email="x"), "INBOX", cut)) == cut
