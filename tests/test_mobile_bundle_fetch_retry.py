"""A transient instance error must not turn an otherwise valid APK build red."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mobile_shell_fetch_retries_transient_http_and_network_failures():
    source = (ROOT / "mobile" / "build-www.sh").read_text(encoding="utf-8")
    call = source[source.index("curl --fail"):source.index("# Inject bundled-mode")]
    assert "--retry 5" in call
    assert "--retry-all-errors" in call
    assert "test -s www/index.html" in call

