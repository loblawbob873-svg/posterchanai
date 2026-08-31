"""Regression coverage for the native Android picture-message composer."""
from pathlib import Path

ROOT = Path(__file__).parents[1]
SMS = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms"


def source(name):
    return (SMS / name).read_text(encoding="utf-8")


def test_attachment_is_copied_to_a_private_durable_draft_and_drawn_immediately():
    draft = source("MmsDraft.java")
    thread = source("ThreadActivity.java")
    layout = (ROOT / "mobile/android/app/src/main/res/layout/sms_thread.xml").read_text()
    assert 'new File(ctx.getFilesDir(), "mms-drafts")' in draft
    assert "out.getFD().sync()" in draft
    assert "tmp.renameTo(dst)" in draft
    assert "stageAttachment(readAttachment(in)" in thread
    assert "paintAttachmentDraft();" in thread
    assert 'android:id="@+id/pc_th_attachment_preview"' in layout


def test_draft_is_restored_after_navigation_or_process_recreation():
    thread = source("ThreadActivity.java")
    restore = thread[thread.index("private void restoreAttachmentDraft"):
                     thread.index("private void clearAttachmentDraft")]
    assert "MmsDraft.load(this, address)" in restore
    assert thread.count("restoreAttachmentDraft();") >= 3


def test_accepted_send_keeps_preview_and_has_durable_explicit_result_states():
    thread = source("ThreadActivity.java")
    receiver = source("MmsSendReceiver.java")
    draft = source("MmsDraft.java")
    send = thread[thread.index("private void sendMms(String body)"):
                  thread.index("private void call()")]
    assert "MmsDraft.SENDING" in send
    assert "clearAttachmentDraft()" not in send
    assert "MmsDraft.SENT" in receiver
    assert "MmsDraft.FAILED" in receiver
    assert "MmsDraft.UNKNOWN" in receiver
    assert 'UNKNOWN = "delivery unknown"' in draft


def test_second_send_is_blocked_while_result_is_pending_or_unknown():
    thread = source("ThreadActivity.java")
    send = thread[thread.index("private void send() {"):
                  thread.index("private void pickAttachment()")]
    assert "!MmsDraft.READY.equals(attachmentDraft.state)" in send
    assert "!MmsDraft.FAILED.equals(attachmentDraft.state)" in send
    assert send.index("say(attachmentDraft.state); return;") < send.index("sendMms(body);")
