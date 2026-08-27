import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/MmsResult.java"
RECEIVER = (SRC.parent / "MmsSendReceiver.java").read_text()
FAILURES = (SRC.parent / "MmsFailures.java").read_text()


def test_sent_failed_unknown_video_and_retry_mapping_executes():
    harness = """package place.poster.app.sms;
public class Probe { public static void main(String[] x) {
 int[] got={MmsResult.classify(-1,0,4),MmsResult.classify(0,0,2),
  MmsResult.classify(0,200,4),MmsResult.classify(0,0,4),
  MmsResult.classify(8,0,4),MmsResult.classify(0,0,5)};
 for(int n:got)System.out.print(n+",");
}}"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "place/poster/app/sms"
        base.mkdir(parents=True)
        (base / "MmsResult.java").write_text(SRC.read_text())
        (base / "Probe.java").write_text(harness)
        built = subprocess.run(["javac", "-d", td, str(base / "MmsResult.java"), str(base / "Probe.java")],
                               capture_output=True, text=True, timeout=20)
        assert built.returncode == 0, built.stderr
        run = subprocess.run(["java", "-cp", td, "place.poster.app.sms.Probe"],
                             capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    # ordinary sent; provider-confirmed sent; video/MMSC 2xx; true unknown; data failure; failed row
    assert run.stdout == "1,1,1,0,2,2,"


def test_receiver_uses_provider_and_http_before_showing_unknown():
    assert "MmsResult.classify(result, http, providerBox)" in RECEIVER
    assert "if (!unknown)" in RECEIVER
    assert "if (!unknown) SmsPlugin.onSendResult" in RECEIVER


def test_ambiguous_callback_does_not_claim_apn_failure_or_offer_blind_retry():
    assert "carrier send status is pending" in FAILURES
    assert "it may already have been sent" in FAILURES
    assert "verify mobile data and the carrier MMS APN" not in FAILURES
    assert 'error.startsWith("delivery unknown")' in FAILURES
