import os
import subprocess
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "mobile/android/app/src/main/java/place/poster/app/sms/MmsAttachment.java")


def test_attachment_reader_runtime_contract():
    harness = r'''
package place.poster.app.sms;
import java.io.*;
public final class MmsAttachmentHarness {
  static void yes(boolean value, String message) { if (!value) throw new AssertionError(message); }
  public static void main(String[] args) throws Exception {
    byte[] image = new byte[1536];
    yes(MmsAttachment.read(new ByteArrayInputStream(image), 2048).length == image.length,
        "ordinary image changed while staging");

    try {
      MmsAttachment.rejectKnownSize(4097, 4096);
      throw new AssertionError("known oversized video accepted");
    } catch (MmsAttachment.TooLarge expected) {
      yes(expected.size == 4097 && expected.limit == 4096, "known size/limit lost");
    }

    // OpenableColumns.SIZE is allowed to be null. Reading one byte beyond the carrier ceiling is
    // the authoritative answer for such a provider and must not allocate the complete video.
    try {
      MmsAttachment.read(new ByteArrayInputStream(new byte[5000]), 4096);
      throw new AssertionError("unknown-size oversized stream accepted");
    } catch (MmsAttachment.TooLarge expected) {
      yes(expected.limit == 4096 && expected.size > expected.limit, "stream limit not reported");
    }

    InputStream revoked = new InputStream() {
      public int read() throws IOException { throw new IOException("permission revoked"); }
      public int read(byte[] b) throws IOException { throw new IOException("permission revoked"); }
    };
    try {
      MmsAttachment.read(revoked, 4096);
      throw new AssertionError("revoked provider accepted");
    } catch (MmsAttachment.TooLarge wrong) {
      throw new AssertionError("revoked provider mislabeled oversized");
    } catch (IOException expected) { }

    String guidance = MmsAttachment.tooLargeMessage(5 * 1024 * 1024L, 300 * 1024);
    yes(guidance.contains("5.0 MB") && guidance.contains("300 KB"), "guidance lacks sizes");
    yes(guidance.contains("Trim or compress"), "guidance has no recovery action");
  }
}
'''
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "MmsAttachmentHarness.java")
        with open(path, "w", encoding="utf-8") as out:
            out.write(harness)
        built = subprocess.run(["javac", "-d", td, SRC, path], text=True, capture_output=True)
        assert built.returncode == 0, built.stderr
        ran = subprocess.run(["java", "-cp", td, "place.poster.app.sms.MmsAttachmentHarness"],
                             text=True, capture_output=True)
        assert ran.returncode == 0, ran.stderr


def test_picker_honors_document_grant_and_nullable_metadata():
    thread = open(os.path.join(ROOT,
        "mobile/android/app/src/main/java/place/poster/app/sms/ThreadActivity.java"),
        encoding="utf-8").read()
    assert "FLAG_GRANT_PERSISTABLE_URI_PERMISSION" in thread
    assert "OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE" in thread
    assert "if (!c.isNull(1))" in thread
    assert "MmsAttachment.rejectKnownSize" in thread
    assert "catch (SecurityException denied)" in thread
    assert "sms_attachment_permission" in thread


def test_picker_stages_a_private_draft_before_uri_grant_can_expire():
    thread = open(os.path.join(ROOT,
        "mobile/android/app/src/main/java/place/poster/app/sms/ThreadActivity.java"),
        encoding="utf-8").read()
    result = thread[thread.index("if (request != PICK_MMS_IMAGE"):
                    thread.index("private byte[] readAttachment")]
    assert result.index("openInputStream(picked)") < result.index("stageAttachment(")
    assert "static Value save" in open(os.path.join(ROOT,
        "mobile/android/app/src/main/java/place/poster/app/sms/MmsDraft.java"), encoding="utf-8").read()
    assert "restoreAttachmentDraft();" in thread
