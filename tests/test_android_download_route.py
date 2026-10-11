"""A download the WebView starts goes to the PAGE when the page is the only one that can read it.

Reported on the APK (member Vyram, 2026-10-11): after "Convert to MP3" in the AI chat, the ORANGE
Download button saved the song, but the ⋮ → Download built into the MP3 PLAYER "fails to download &
adds an incomplete file to the phone". MainActivity's DownloadListener handed every http(s) download
straight to Android's DownloadManager with nothing but a User-Agent -- no session cookie, no Bearer
token -- and an AI-chat artifact (`/api/files/...`) is behind the user's auth. DownloadManager got
401, Android said "download failed" and left a stub in Downloads.

The rule (DownloadRoute.decide, pure, compiled and RUN here): a URL on THIS app's instance, or on the
WebView's own origin, is handed back to the page (`pcNativeDownload`), which fetches it WITH the app's
auth and saves the full bytes through MediaSave. blob:/data: always go to the page (only the page can
read them). Anything else -- a post's video on somebody else's media host -- keeps DownloadManager,
which streams a 2 GB file to disk without holding it in memory, and needs no auth there.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "mobile/android/app/src/main/java/place/poster/app")
JAVAC, JAVA = shutil.which("javac"), shutil.which("java")

H = r"""
import place.poster.app.DownloadRoute;
public class DR { public static void main(String[] a){
  String I = "https://poster.place", P = "https://localhost";
  String[][] c = {
    {"https://poster.place/api/files/vyram/5/enc_bbbb.mp3?t=x", I, P},   // own instance artifact
    {"https://POSTER.place:443/api/files/x.mp4", I, P},                  // same origin, other spelling
    {"https://poster.place/api/files/x.mp3", I + "/", P},                // base with a trailing slash
    {"http://example.onion/api/files/x.mp3", "http://example.onion", P}, // a cleartext instance
    {"https://localhost/x.bin", I, P},                                   // the WebView's own origin
    {"blob:https://localhost/1234", I, P},
    {"data:audio/mpeg;base64,SUQz", I, P},
    {"https://nostr.build/v/abc.mp4", I, P},                             // a third-party media host
    {"https://poster.place.evil.com/api/files/x.mp3", I, P},            // a look-alike host
    {"http://poster.place/api/files/x.mp3", I, P},                       // wrong scheme = other origin
    {"https://poster.place:8443/x.mp3", I, P},                           // wrong port = other origin
    {"https://nostr.build/v/abc.mp4", "", P},                            // standalone: no instance
    {"https://poster.place/x.mp3", "", ""},                              // nothing known at all
    {"https://poster.place/x.mp3", "null", P},                           // the JS said null
  };
  StringBuilder o = new StringBuilder();
  for (String[] x : c) o.append(DownloadRoute.decide(x[0], x[1], x[2])).append('\n');
  // What evaluateJavascript hands back: a JSON-encoded string.
  o.append(DownloadRoute.unquote("\"https://poster.place\\nhttps://localhost\"")).append('|')
   .append(DownloadRoute.unquote("null")).append('|')
   .append(DownloadRoute.unquote("\"a\\u003Cb\\\\c\\\"d\\/e\"")).append('\n');
  System.out.print(o);
}}
"""


@pytest.mark.skipif(not (JAVAC and JAVA), reason="JDK not installed")
def test_own_instance_downloads_go_to_the_page_and_others_to_the_download_manager():
    tmp = tempfile.mkdtemp()
    try:
        open(os.path.join(tmp, "DR.java"), "w").write(H)
        r = subprocess.run([JAVAC, "-d", tmp, os.path.join(APP, "DownloadRoute.java"),
                            os.path.join(tmp, "DR.java")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        out = subprocess.run([JAVA, "-cp", tmp, "DR"], capture_output=True, text=True).stdout.split("\n")
        assert out[:14] == [
            "page", "page", "page", "page", "page", "page", "page",
            "manager", "manager", "manager", "manager", "manager", "manager", "manager",
        ], out
        # unquote turns the JSON `\n` back into a real newline, so its answer spans two lines.
        assert out[14] == "https://poster.place", out
        assert out[15] == "https://localhost||a<b\\c\"d/e", out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _listener():
    src = open(os.path.join(APP, "MainActivity.java"), encoding="utf-8").read()
    return src[src.index("private void catchWebViewDownloads()"):]


def test_the_listener_asks_the_page_for_its_instance_and_routes_through_the_rule():
    """The instance is chosen at RUNTIME (localStorage `pc_instance`), so the native side cannot have
    it baked in: it asks the live page, then lets DownloadRoute decide."""
    seg = _listener()
    assert "evaluateJavascript" in seg and "__PC_API_BASE__" in seg, \
        "the native side never learns which instance the page signed in to"
    assert "DownloadRoute.decide(" in seg, "the download is not routed through the tested rule"
    assert "DownloadRoute.PAGE" in seg


def test_the_page_hand_off_carries_the_name_and_type():
    """The page names the file from Content-Disposition and routes it by MIME (Music for an MP3) --
    the WebView knows both, and the page must not have to guess them back."""
    seg = _listener()
    hand = seg[seg.index("private void handToPage("):]
    hand = hand[:hand.index("\n    }\n")]
    assert "contentDisposition" in hand and "mime" in hand, hand


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
