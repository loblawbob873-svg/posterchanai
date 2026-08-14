"""A WebView with no DownloadListener drops every download it starts, and says nothing.

This app had none. That is not a corner case: Android draws its own controls on a `<video>`, those
controls carry a ⋮ → Download, and on a post's video it is the obvious thing to press. Pressed, the
WebView asks its listener what to do, finds none, and does nothing — no file, no error, no toast.
Reported exactly that way: "I try to download video from post and nothing happened."

WHY IT HID FOR SO LONG. Every save button the client draws already routed AROUND the missing
listener: they call `saveBlobAs`, which writes the bytes itself and opens the share sheet, precisely
because a bare `<a download>` is ignored by this same WebView. So the only broken downloads were the
ones Android renders for us, which no test and no code path of ours ever touched.

Gradle only builds in CI, so this checks the WIRING — that the listener is installed, that it is
installed on the bridge's WebView after the bridge exists, and that neither branch of it can end in
silence. The last part is the point: replacing an invisible failure with a different invisible
failure would not be a fix.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                    "place", "poster", "app", "MainActivity.java")
APP_JS = os.path.join(ROOT, "static", "js", "client", "app.js")


def _java():
    with open(MAIN, encoding="utf-8") as fh:
        return fh.read()


def test_the_webview_has_a_download_listener_at_all():
    src = _java()
    assert "setDownloadListener" in src, \
        "no DownloadListener: every download the WebView starts is dropped in silence"
    assert "onDownloadStart" in src


def test_it_is_installed_after_the_bridge_exists():
    """`getBridge()` returns null before `super.onCreate()`, so an early call is a no-op.

    The same trap `allowMediaWithoutAGesture` is written to avoid, and it fails the same way: the
    method runs, throws into its own catch, and the app behaves exactly as if the code were absent.
    """
    src = _java()
    body = src[src.index("public void onCreate("):src.index("public void onCreate(") + 1400]
    sup = body.index("super.onCreate(")
    call = body.index("catchWebViewDownloads()")
    assert sup < call, "catchWebViewDownloads() runs before the bridge (and its WebView) exists"


def test_http_downloads_go_to_the_platform_downloader():
    """DownloadManager, not a hand-rolled fetch: it survives backgrounding, retries, writes to the
    public Downloads folder and posts its own notification."""
    src = _java()
    seg = src[src.index("private void catchWebViewDownloads()"):]
    assert "DownloadManager.Request" in seg and "enqueue" in seg
    assert "DIRECTORY_DOWNLOADS" in seg, "the file lands somewhere the user cannot find"
    assert "guessFileName" in seg, \
        "no filename derivation, so a Blossom URL saves as a bare sha256 with no extension"


def test_blob_urls_are_handed_back_to_the_page_not_dropped():
    """DownloadManager cannot read a `blob:` — only the page that minted it can.

    Dropping them here would recreate the exact bug this listener exists to fix, one layer down.
    """
    src = _java()
    seg = src[src.index("private void catchWebViewDownloads()"):]
    assert 'startsWith("blob:")' in seg and 'startsWith("data:")' in seg
    assert "pcNativeDownload" in seg, "blob downloads are recognised and then discarded"


def test_the_page_actually_listens_for_that_hand_back():
    """The other half. An event nobody hears is the same silence, moved."""
    js = open(APP_JS, encoding="utf-8").read()
    assert "'pcNativeDownload'" in js, "the native side hands blob downloads to nobody"
    seg = js[js.index("'pcNativeDownload'"): js.index("'pcNativeDownload'") + 700]
    assert "saveBlobAs" in seg, "the hand-back does not reach the app's save pipeline"


def test_a_failed_download_says_so():
    """The whole defect was a failure with no sign of itself."""
    src = _java()
    seg = src[src.index("private void catchWebViewDownloads()"):]
    catch = seg[seg.index("catch (Throwable t)"):]
    assert "Toast" in catch[:400], "a download that cannot start fails as silently as before"


def test_the_imports_it_needs_are_present():
    """Java resolves these at COMPILE time and Gradle only runs in CI, so a missing import here is a
    broken APK build discovered an hour later rather than now."""
    src = _java()
    for imp in ("android.app.DownloadManager", "android.net.Uri", "android.os.Environment",
                "android.webkit.DownloadListener", "android.webkit.URLUtil",
                "android.widget.Toast", "org.json.JSONObject"):
        assert re.search(r"^import\s+" + re.escape(imp) + r";", src, re.M), f"missing import: {imp}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
