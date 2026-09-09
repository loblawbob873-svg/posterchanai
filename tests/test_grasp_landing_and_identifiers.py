"""GRASP-01's two smaller MUSTs/SHOULDs on the git smart-HTTP service.

  - "/<npub>/<percent-encoded-identifier>.git" — ngit v3 percent-encodes reserved characters in both
    `nostr://` clone URLs and GRASP HTTP paths (CHANGELOG line 487), and we read the raw segment, so
    an identifier that needed any encoding 404'd.
  - "SHOULD serve a webpage at the same endpoint linking to git nostr client(s) … and a 404 page for
    repositories it doesn't host." `GET https://poster.place/git/<npub>/<id>.git` returned a JSON
    404 — the clone URL is the one address of a repository people paste to each other, and pasting
    it into a browser said it did not exist.
"""
from __future__ import annotations

import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import git_host_main as gh                             # noqa: E402
from app.services import git_host_service as ghs       # noqa: E402
from app.services.nostr import bip340, nostr_service   # noqa: E402

OWNER = bip340.pubkey_from_seckey((11).to_bytes(32, "big")).hex()
NPUB = nostr_service.npub_of(OWNER)
BASE = "https://poster.place/git"


# ---------------------------------------------------------------- percent-encoded identifiers

def test_a_percent_encoded_identifier_is_decoded():
    """`my%2Drepo` is `my-repo`. We never decoded, so it 404'd."""
    got = gh._parse_repo_path("/git/%s/my%%2Drepo.git/info/refs" % NPUB)
    assert got == (OWNER, "my-repo", "info/refs"), got


def test_the_owner_segment_is_decoded_too():
    got = gh._parse_repo_path("/git/%s/ok.git/info/refs" % NPUB)
    assert got and got[0] == OWNER


@pytest.mark.parametrize("seg", ["a%2F..%2Fb", "%2E%2E", "%2e%2e%2f%2e%2e", "%2Fetc%2Fpasswd",
                                 "..%2Fother", "%00null"])
def test_DECODING_CANNOT_OPEN_A_TRAVERSAL(seg):
    """THE WHOLE SAFETY ARGUMENT IS THE ORDER: decode first, `sanitize_repo_id` second. `%2F`
    decodes to `/` and `%2E%2E` to `..`, which is exactly what the sanitizer exists to refuse — so
    decoding cannot widen the filesystem allowlist, and the allowlist is not relaxed to accommodate
    any decoded character."""
    assert gh._parse_repo_path("/git/%s/%s.git/info/refs" % (NPUB, seg)) is None


def test_an_identifier_that_is_not_a_slug_after_decoding_is_still_refused():
    """A DECISION NOT TAKEN, recorded rather than guessed. NIP-34's own examples include
    `my%20%F0%9F%9A%80%20repo`, which decodes to a name containing spaces and an emoji — legal as an
    identifier, illegal as a directory under our `[a-z0-9._-]` allowlist. Supporting those needs an
    on-disk NAMING scheme (a hash, or a strict re-encoding) and therefore a migration story; it is
    not a regex edit, and widening the allowlist is the one thing that must not be done casually,
    since it is the path-traversal boundary. Decoding is shipped; the naming scheme is asked about."""
    assert gh._parse_repo_path("/git/%s/my%%20%%F0%%9F%%9A%%80%%20repo.git/info/refs" % NPUB) is None


# ---------------------------------------------------------------- the landing page

@pytest.fixture
def serve(tmp_path, monkeypatch):
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(gh, "_CONFIG", {
        "pg_dsn": "", "public_base": BASE, "allowlist": "", "auto_provision": False,
        "accept_policy": "local-or-wot", "read_skew": 60, "write_skew": 120, "port": 0},
        raising=False)
    gh._prov_deny.clear()
    gh._priv_cache.clear()
    gh._alias_cache.clear()

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = _S(("127.0.0.1", 0), gh._Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()

    def get(path, headers=None):
        req = urllib.request.Request("http://127.0.0.1:%d/git/%s/%s" % (port, NPUB, path))
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

    yield get
    httpd.shutdown()


def test_the_clone_url_in_a_browser_serves_a_page(serve):
    ghs.create_repo(OWNER, "demo")
    code, body, headers = serve("demo.git")
    assert code == 200, body[:200]
    assert headers.get("Content-Type", "").startswith("text/html")
    assert "<!doctype html>" in body.lower()


def test_the_page_names_a_nostr_git_client_and_the_clone_command(serve):
    """"linking to git nostr client(s)" is the SHOULD; the clone command is why anybody pasted the
    URL in the first place."""
    ghs.create_repo(OWNER, "demo")
    _c, body, _h = serve("demo.git")
    assert "gitworkshop.dev" in body and "ngit" in body
    assert "git clone %s/%s/demo.git" % (BASE, NPUB) in body
    assert "nostr://%s/demo" % NPUB in body


def test_the_page_links_to_this_instances_own_repo_view(serve):
    """`/r/<npub>/<repo-id>` is the app's repo page and the only route that builds a link preview."""
    ghs.create_repo(OWNER, "demo")
    _c, body, _h = serve("demo.git")
    assert '/r/%s/demo' % NPUB in body


def test_a_repo_we_do_not_host_gets_a_404_PAGE_that_is_still_a_404(serve):
    """"and a 404 page for repositories it doesn't host" — a page, with the status kept, so a
    monitor still sees 404 and a person still sees an explanation."""
    code, body, headers = serve("nope.git")
    assert code == 404
    assert headers.get("Content-Type", "").startswith("text/html")
    assert "gitworkshop.dev" in body


def test_every_OTHER_route_still_gets_the_plain_machine_readable_404(serve):
    """The page is only for the bare clone URL. A git client asking for a repo we do not host must
    still get the terse refusal it expects, not a wall of HTML."""
    code, body, headers = serve("nope.git/info/refs?service=git-upload-pack")
    assert code == 404
    assert not headers.get("Content-Type", "").startswith("text/html"), body[:120]
    code, _b, headers = serve("nope.git/tree/HEAD")
    assert code == 404 and not headers.get("Content-Type", "").startswith("text/html")


def test_the_landing_page_is_READ_GATED_like_a_clone(serve):
    """A private repo's page must not confirm the repository exists, describe it, or hand out its
    clone command — it is the same disclosure the read gate refuses for the bytes."""
    ghs.create_repo(OWNER, "secret", private=True)
    code, body, headers = serve("secret.git")
    assert code == 401, body[:200]
    assert "secret" not in body or "authentication" in body.lower()
    assert any("nostr" in v.lower() for k, v in headers.items() if k.lower() == "www-authenticate")


def test_the_page_escapes_what_it_interpolates(serve):
    """`repo_id` has passed `sanitize_repo_id` and the npub is derived from validated hex, so nothing
    hostile can reach here today — but "it cannot contain a quote" is exactly the assumption the
    30617 share card had to learn not to make, and this page is one refactor away from rendering a
    name somebody else chose."""
    import html as _html
    ghs.create_repo(OWNER, "demo")
    _c, body, _h = serve("demo.git")
    assert "<script" not in body.lower()
    assert _html.escape(NPUB) in body


def test_a_smart_http_request_is_untouched_by_the_page(serve):
    """The page hangs off a request with no `service=` param, which is not smart-HTTP — no git
    client depends on what the bare URL returns, and this proves the real endpoint still answers."""
    r = ghs.create_repo(OWNER, "demo")
    assert r.get("ok")
    code, body, _h = serve("demo.git/info/refs?service=git-upload-pack")
    assert code == 200 and "service=git-upload-pack" in body
