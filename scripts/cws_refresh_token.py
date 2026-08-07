#!/usr/bin/env python3
"""Mint the Chrome Web Store refresh token that CI publishes with. Run this ONCE, by hand.

    python3 scripts/cws_refresh_token.py

CI cannot do this step: minting a refresh token needs a human to click "Allow" in a browser, and
after that the token is what the workflow uses forever. So this walks the consent flow, exchanges
the code, and prints the four values to paste into GitHub Secrets.

BEFORE running, in the Google Cloud console (a free project is fine):
  1. Enable the **Chrome Web Store API** for the project.
  2. Create an **OAuth 2.0 Client ID** of type **Desktop app** → gives a client id + secret.
  3. On the OAuth consent screen, set the publishing status to **In production**.

Step 3 is not optional and is the trap this script exists to warn about: while the consent screen
is in "Testing", Google expires refresh tokens after **SEVEN DAYS**. The workflow then starts
failing about a week after it was set up, long after anyone connects the two events, and the fix is
not in the workflow at all. "In production" needs no verification review for a client only you use.

The item ID is the last path segment of your extension's Web Store URL:
    https://chrome.google.com/webstore/detail/<name>/<THIS BIT>
It exists only after the FIRST upload, which has to be done by hand in the dashboard.
"""
import json
import sys
import urllib.parse
import urllib.request

# "urn:ietf:wg:oauth:2.0:oob" is dead (Google turned it off in 2022). A Desktop-app client is
# allowed to use a loopback redirect, and http://localhost is accepted without the port having to be
# registered — we never actually serve it; the code is copied out of the browser's address bar.
REDIRECT = "http://localhost"
SCOPE = "https://www.googleapis.com/auth/chromewebstore"
AUTH = "https://accounts.google.com/o/oauth2/auth"
TOKEN = "https://oauth2.googleapis.com/token"


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"\n  Google said {e.code}: {detail}\n", file=sys.stderr)
        if "invalid_grant" in detail:
            print("  invalid_grant almost always means the code was already used or has expired —\n"
                  "  they are single-use and short-lived. Re-run and paste a fresh one.\n",
                  file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    print(__doc__)
    client_id = input("OAuth client ID:     ").strip()
    client_secret = input("OAuth client secret: ").strip()
    item_id = input("Web Store item ID:   ").strip()
    if not (client_id and client_secret):
        raise SystemExit("client id and secret are both required")

    url = AUTH + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "scope": SCOPE,
        # Both are required to get a REFRESH token rather than only an access token. Without
        # access_type=offline Google returns a token good for an hour and nothing renewable, and
        # without prompt=consent it silently omits the refresh token on every grant after the first
        # — which looks like the script is broken when it is the account remembering you.
        "access_type": "offline",
        "prompt": "consent",
    })
    print("\n1. Open this in a browser, sign in as the Web Store account, and click Allow:\n")
    print("   " + url)
    print("\n2. The browser will fail to load a localhost page — that is expected. Copy the\n"
          "   `code=` value out of its address bar (everything up to the next `&`).\n")
    code = input("code: ").strip()
    if code.startswith("http"):   # pasted the whole URL — take the code out of it
        q = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
        code = (q.get("code") or [""])[0]
    if not code:
        raise SystemExit("no code")

    tok = _post(TOKEN, {
        "code": urllib.parse.unquote(code),
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    })
    refresh = tok.get("refresh_token")
    if not refresh:
        raise SystemExit(
            "Google returned no refresh_token. That happens when the account has already granted\n"
            "this client and `prompt=consent` was dropped, or when access_type=offline was missing.\n"
            "Revoke the app at https://myaccount.google.com/permissions and run this again.")

    print("\nDone. Add these four as GitHub Actions secrets "
          "(Settings → Secrets and variables → Actions):\n")
    print(f"  CWS_CLIENT_ID       {client_id}")
    print(f"  CWS_CLIENT_SECRET   {client_secret}")
    print(f"  CWS_REFRESH_TOKEN   {refresh}")
    print(f"  CWS_ITEM_ID         {item_id or '<the id from your Web Store URL>'}")
    print("\nOr, with the gh CLI:\n")
    for k, v in (("CWS_CLIENT_ID", client_id), ("CWS_CLIENT_SECRET", client_secret),
                 ("CWS_REFRESH_TOKEN", refresh), ("CWS_ITEM_ID", item_id)):
        print(f"  printf '%s' '{v}' | gh secret set {k}")
    print("\nThe refresh token does not expire — UNLESS the OAuth consent screen is still in\n"
          "'Testing', in which case Google kills it after 7 days. Set it to 'In production'.\n")


if __name__ == "__main__":
    main()
