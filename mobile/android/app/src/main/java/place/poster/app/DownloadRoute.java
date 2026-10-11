package place.poster.app;

import java.net.URI;
import java.util.Locale;

/**
 * WHO SAVES A DOWNLOAD THE WEBVIEW STARTED -- the page, or Android's DownloadManager. Pure, so a test
 * can compile and run it without Android (tests/test_android_download_route.py).
 *
 * DownloadManager fetches the URL ITSELF, with no session cookie and no Bearer token. That is right for
 * a post's video on somebody else's media host (public, possibly huge -- it streams to disk and
 * survives the app being backgrounded) and wrong for anything on this app's own instance: an AI-chat
 * artifact (`/api/files/...`) is behind the user's auth, so DownloadManager got a 401, Android said
 * "download failed", and a stub was left in Downloads. Reported as the ⋮ → Download on the MP3 player
 * failing while the app's own Download button next to it worked.
 *
 * So: blob:/data:, the WebView's own origin and the signed-in instance's origin go to the PAGE, which
 * fetches with the app's auth and saves through MediaSave. Everything else goes to the MANAGER.
 */
public final class DownloadRoute {
  private DownloadRoute() { }

  public static final String PAGE = "page";
  public static final String MANAGER = "manager";

  /**
   * @param url          what the WebView wants to download
   * @param instanceBase the instance the page talks to (`window.__PC_API_BASE__`), or "" when unknown
   * @param pageOrigin   the page's own origin (`location.origin`, https://localhost in the APK), or ""
   */
  public static String decide(String url, String instanceBase, String pageOrigin) {
    String u = url == null ? "" : url.trim();
    String low = u.toLowerCase(Locale.ROOT);
    if (low.startsWith("blob:") || low.startsWith("data:")) return PAGE;
    String o = origin(u);
    if (o.isEmpty()) return MANAGER;
    if (o.equals(origin(instanceBase)) || o.equals(origin(pageOrigin))) return PAGE;
    return MANAGER;
  }

  /** scheme://host:port, lower-cased, default port filled in; "" for anything that is not http(s). */
  static String origin(String s) {
    if (s == null) return "";
    String t = s.trim();
    if (t.isEmpty() || "null".equals(t)) return "";
    try {
      URI u = new URI(t);
      String scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase(Locale.ROOT);
      String host = u.getHost() == null ? "" : u.getHost().toLowerCase(Locale.ROOT);
      if (host.isEmpty() || !("http".equals(scheme) || "https".equals(scheme))) return "";
      int port = u.getPort() >= 0 ? u.getPort() : ("https".equals(scheme) ? 443 : 80);
      return scheme + "://" + host + ":" + port;
    } catch (Exception e) {
      return "";
    }
  }

  /**
   * What `WebView.evaluateJavascript` hands back is the result JSON-ENCODED: a string arrives with its
   * quotes and escapes, `undefined`/`null` as the bare word null. Returns the plain string, "" for null.
   */
  public static String unquote(String json) {
    if (json == null) return "";
    String s = json.trim();
    if (s.length() < 2 || s.charAt(0) != '"' || s.charAt(s.length() - 1) != '"') return "";
    StringBuilder b = new StringBuilder();
    for (int i = 1; i < s.length() - 1; i++) {
      char c = s.charAt(i);
      if (c != '\\' || i + 1 >= s.length() - 1) { b.append(c); continue; }
      char n = s.charAt(++i);
      switch (n) {
        case 'n': b.append('\n'); break;
        case 't': b.append('\t'); break;
        case 'r': b.append('\r'); break;
        case 'b': b.append('\b'); break;
        case 'f': b.append('\f'); break;
        case 'u':
          if (i + 4 <= s.length() - 2) {   // four hex digits before the closing quote
            try { b.append((char) Integer.parseInt(s.substring(i + 1, i + 5), 16)); i += 4; }
            catch (NumberFormatException e) { b.append('u'); }
          } else b.append('u');
          break;
        default: b.append(n);   // \" \\ \/
      }
    }
    return b.toString();
  }
}
