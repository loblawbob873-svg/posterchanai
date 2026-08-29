#!/usr/bin/env python3
"""Source archives, the readable repo URL, and what the git PROXY is allowed to forward.

Three things a repo needs before a link to it is worth sending someone, each verified to fail
without its fix:

  ARCHIVE  `GET /<owner>/<id>.git/archive/<ref>.tar.gz|.zip` — the "Download source" every forge has,
           and the only way to get a copy of a project without installing git. Run against the REAL
           host over a REAL repo, because the interesting cases are all about bytes: an archive that
           unpacks into its own directory, a `?ref=` that reaches a branch whose name has a slash in
           it, and — the one that motivated the pre-flight rev-parse — a MISSING ref answering 404
           instead of a 200 whose body is empty, which a browser saves as a zero-byte "download" with
           no error anywhere.

  SHARE    `git_share.resolve_owner` decides what `/r/<owner>/<repo>` means. It accepts an npub, a
           hex key and a NIP-05 name granted on this node, and it must never resolve junk — the owner
           segment picks WHOSE repo is served.

  PROXY    the smart-HTTP proxy's allowlist. The browse routes were absent from it, so a repo could
           be cloned through a proxy node but not read through one; the test pins both halves — the
           browse routes pass, and a path that is not a git route still cannot turn this into an open
           proxy.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _git(*args, cwd=None, **kw):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, **kw)
    if p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (args[:2], p.stderr.decode()[:400]))
    return p


@pytest.fixture(scope="module")
def host():
    """A real bare repo served by the real git host. Yields (base_url, owner_npub)."""
    store = tempfile.mkdtemp(prefix="grasp-archive-test-")
    wt = tempfile.mkdtemp(prefix="grasp-archive-wt-")
    os.environ["GRASP_GIT_PROJECT_ROOT"] = store
    from app.services.nostr import nostr_service

    sk = os.urandom(32)
    owner_hex = nostr_service.derive_pubkey(sk)
    owner = nostr_service.npub_of(owner_hex)
    # `git init --bare` directly, NOT git_host_service.create_repo: create_repo installs the
    # pre-receive hook, which fails closed with no relay DSN and would reject the seeding push. The
    # archive route is a READ, so the push authorization is not what is under test here.
    repo = os.path.join(store, owner_hex, "demo.git")
    os.makedirs(os.path.dirname(repo), exist_ok=True)
    _git("init", "-q", "--bare", repo)

    _git("init", "-q", "-b", "master", wt)
    _git("config", "user.email", "t@t", cwd=wt)
    _git("config", "user.name", "T", cwd=wt)
    os.makedirs(os.path.join(wt, "src"), exist_ok=True)
    open(os.path.join(wt, "README.md"), "w").write("# demo\n")
    open(os.path.join(wt, "src", "a.py"), "w").write("print('hi')\n")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "first", cwd=wt)
    _git("branch", "feature/x", cwd=wt)
    _git("tag", "v1", cwd=wt)
    _git("remote", "add", "o", repo, cwd=wt)
    _git("push", "-q", "o", "master", "feature/x", "v1", cwd=wt)

    import git_host_main as gh
    gh._CONFIG = {"pg_dsn": "", "read_skew": 300, "write_skew": 120, "port": 0,
                  "repo_max_mb": 512, "allow_force": True, "nip98_push": True, "public_base": ""}

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = _S(("127.0.0.1", 0), gh._Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    base = "http://127.0.0.1:%d/%s/demo.git" % (httpd.server_address[1], owner)
    try:
        yield base, owner
    finally:
        httpd.shutdown()
        shutil.rmtree(wt, ignore_errors=True)
        shutil.rmtree(store, ignore_errors=True)
        os.environ.pop("GRASP_GIT_PROJECT_ROOT", None)


def _get(url):
    try:
        r = urllib.request.urlopen(url, timeout=30)
        return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


# ---------------------------------------------------------------- archive


def test_targz_archive_unpacks_into_its_own_directory(host):
    base, _ = host
    st, body, hdrs = _get(base + "/archive/master.tar.gz")
    assert st == 200, body[:200]
    # An archive with no prefix sprays a repo over whatever directory it is opened in. Every entry
    # must sit under one top-level folder named for the repo and the ref.
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
        names = tf.getnames()
    assert names, "empty tarball"
    assert all(n == "demo-master" or n.startswith("demo-master/") for n in names), names[:5]
    assert "demo-master/README.md" in names
    assert "demo-master/src/a.py" in names
    # ...and the browser must be told what to call it, or the file lands named after the URL.
    assert 'filename="demo-master.tar.gz"' in (hdrs.get("Content-Disposition") or "")


def test_zip_archive_is_a_real_zip(host):
    base, _ = host
    st, body, hdrs = _get(base + "/archive/master.zip")
    assert st == 200
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
        assert zf.testzip() is None
    assert any(n == "demo-master/README.md" for n in names), names[:5]
    assert "application/zip" in (hdrs.get("Content-Type") or "")


def test_ref_query_reaches_a_branch_whose_name_contains_a_slash(host):
    """`archive/feature/x.tar.gz` is unspellable as one path segment — `?ref=` is the escape hatch
    every other browse route here already has, and the archive route has to honour the same one."""
    base, _ = host
    st, body, hdrs = _get(base + "/archive/HEAD.tar.gz?ref=feature/x")
    assert st == 200, body[:200]
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
        names = tf.getnames()
    assert all(n == "demo-feature-x" or n.startswith("demo-feature-x/") for n in names), names[:5]


def test_a_tag_archives_too(host):
    base, _ = host
    st, body, _ = _get(host[0] + "/archive/v1.tar.gz")
    assert st == 200
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
        assert "demo-v1/README.md" in tf.getnames()


def test_missing_ref_is_a_404_and_not_an_empty_200(host):
    """THE failure this route is shaped around. `git archive` on a ref that does not exist writes its
    complaint to stderr and exits nonzero having produced NO bytes — after the 200 and the headers
    have already gone out. That saves as a zero-byte file with nothing in any log to say why, so the
    ref is resolved BEFORE the response starts."""
    base, _ = host
    st, body, _ = _get(base + "/archive/no-such-branch.tar.gz")
    assert st == 404, (st, body[:200])
    assert body, "a 404 must say something"


def test_unknown_format_is_refused(host):
    base, _ = host
    assert _get(base + "/archive/master.rar")[0] == 400
    assert _get(base + "/archive/master")[0] == 400


def test_hostile_refs_never_reach_git(host):
    """A ref must stay a ref: anything that git would read as an option or as revision syntax is
    refused by the shared `_valid_ref`, so the archive route inherits the same bound as /tree /log."""
    base, _ = host
    for bad in ("--upload-pack=touch+x", "..", "master@{1}", "master^", "HEAD~2", "a:b"):
        st, _, _ = _get(base + "/archive/HEAD.tar.gz?ref=" + urllib.parse.quote(bad, safe=""))
        assert st in (400, 404), "ref %r was not refused (%d)" % (bad, st)


def test_archive_sends_cors_like_every_other_route(host):
    """An in-browser Nostr git client reads a repo straight from its announced clone URL. Without the
    header it can render the announcement and never the code — which is why the host sets it on every
    response, and why a new route that forgets it is a silent regression for those clients only."""
    base, _ = host
    _, _, hdrs = _get(base + "/archive/master.tar.gz")
    assert hdrs.get("Access-Control-Allow-Origin") == "*"


# ---------------------------------------------------------------- the file finder's index


def test_paths_lists_every_file_flat(host):
    """The finder searches this list, so anything missing from it is a file that "does not exist"
    to a person who knows perfectly well that it does."""
    base, _ = host
    st, body, _ = _get(base + "/paths/master")
    assert st == 200, body[:200]
    j = json.loads(body)
    assert sorted(j["paths"]) == ["README.md", "src/a.py"]
    assert j["count"] == 2 and j["truncated"] is False
    # Paths only. Sizes and per-file commits are what /tree pays a history walk for; if they ever
    # appear here, the search box has quietly become the slowest thing on the page.
    assert isinstance(j["paths"][0], str)


def test_paths_follows_the_ref(host):
    base, _ = host
    st, body, _ = _get(base + "/paths/HEAD?ref=v1")
    assert st == 200
    assert "README.md" in json.loads(body)["paths"]
    assert _get(base + "/paths/nope")[0] == 404
    assert _get(base + "/paths/HEAD?ref=" + urllib.parse.quote("--exec=x", safe=""))[0] == 400


# ---------------------------------------------------------------- share / owner resolution


def test_resolve_owner_accepts_the_forms_a_person_can_type():
    from app.services import git_share
    from app.services.nostr import nostr_service
    hexed = "4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6"
    npub = nostr_service.npub_of(hexed)
    assert git_share.resolve_owner(npub) == hexed
    assert git_share.resolve_owner(hexed) == hexed
    assert git_share.resolve_owner(hexed.upper()) == hexed


def test_resolve_owner_refuses_junk():
    """The owner segment decides WHOSE repo is served, so anything unrecognised must resolve to
    nobody rather than to a guess."""
    from app.services import git_share
    for bad in ("", "   ", "npub1notreallyakey", "../../etc/passwd", "x" * 300, "nobody@example.com"):
        assert git_share.resolve_owner(bad) is None, bad


def test_resolve_owner_accepts_only_this_nodes_qualified_nip05(monkeypatch):
    """A foreign domain must never be discarded and rebound to a same-named local account."""
    from app.services import git_share, settings_store

    alice = "4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6"
    values = {
        "nostr_relay_nip05_domain": "Poster.Place",
        "nostr_relay_nip05_names": "alice=" + alice,
    }
    monkeypatch.setattr(settings_store, "get", lambda key, default="": values.get(key, default))

    assert git_share.resolve_owner("alice") == alice
    assert git_share.resolve_owner("alice@poster.place") == alice
    assert git_share.resolve_owner("alice@POSTER.PLACE.") == alice
    assert git_share.resolve_owner("alice@evil.example") is None
    assert git_share.resolve_owner("alice@") is None
    assert git_share.resolve_owner("alice@@poster.place") is None


def test_qualified_nip05_is_refused_when_instance_domain_is_unconfigured(monkeypatch):
    from app.services import git_share, settings_store

    alice = "4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6"
    values = {"nostr_relay_nip05_names": "alice=" + alice}
    monkeypatch.setattr(settings_store, "get", lambda key, default="": values.get(key, default))

    assert git_share.resolve_owner("alice") == alice
    assert git_share.resolve_owner("alice@poster.place") is None


def test_a_nip05_owner_resolves_to_LOWERCASE_hex(monkeypatch):
    """Same rule as the npub branch, and for the same reason: this value goes into a relay `authors`
    filter, which is matched byte-for-byte. A settings line written with an uppercase key would make
    every lookup for that owner miss and the page preview nothing."""
    from app.services import git_share
    from app.services.nostr_relay import thread as _thread
    hexed = "4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6"
    monkeypatch.setattr(_thread, "_parse_nip05", lambda raw, d: ({"Alice": hexed.upper()}, None))
    assert git_share.resolve_owner("alice") == hexed
    assert git_share.resolve_owner("ALICE") == hexed
    assert git_share.resolve_owner("nobody") is None


def test_a_qualified_nip05_owner_must_name_THIS_instance(monkeypatch):
    """`/r/alice@evil.example/repo` must not open the LOCAL alice's repository.

    The owner segment decides whose repo is served, so a name carrying somebody else's domain has to
    be refused rather than have the domain dropped — otherwise a link that reads as pointing at a
    stranger's account silently opens ours, which is the whole trust story of the URL inverted."""
    from app.services import git_share, settings_store
    from app.services.nostr_relay import thread as _thread
    hexed = "4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6"
    monkeypatch.setattr(_thread, "_parse_nip05", lambda raw, d: ({"alice": hexed}, None))
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: "example.test" if k == "nostr_relay_nip05_domain" else "")
    assert git_share.resolve_owner("alice@example.test") == hexed      # ours, qualified
    assert git_share.resolve_owner("alice@EXAMPLE.TEST.") == hexed     # case + trailing dot
    assert git_share.resolve_owner("alice") == hexed                   # the short local form
    for foreign in ("alice@evil.example", "alice@", "alice@a@b", "alice@ "):
        assert git_share.resolve_owner(foreign) is None, foreign


def test_repo_id_allowlist_matches_the_hosts():
    from app.services import git_share
    assert git_share.valid_repo_id("posterchanai")
    assert git_share.valid_repo_id("my-app.v2_1")
    for bad in ("", "-leading", ".hidden", "Upper", "has space", "a/b", "x" * 101):
        assert not git_share.valid_repo_id(bad), bad


def test_the_url_identifier_is_case_insensitive_but_still_bounded():
    """`valid_repo_id` mirrors what this HOST will mint; `valid_url_repo_id` bounds what a shared URL
    may NAME, which includes repos announced elsewhere with an uppercase `d`. Both must still refuse
    a path segment that is not an identifier at all."""
    from app.services import git_share
    assert git_share.valid_url_repo_id("MyApp")
    assert git_share.valid_url_repo_id("posterchanai")
    assert not git_share.valid_repo_id("MyApp")          # the host would not mint this one
    for bad in ("", "-leading", ".hidden", "has space", "a/b", "..", "x" * 101):
        assert not git_share.valid_url_repo_id(bad), bad


def test_a_dead_relay_is_never_cached_as_no_such_repo(monkeypatch):
    """`_ws_query` returns [] for BOTH "no announcement" and "the relay never answered" unless you
    ask for strict. Without it one blip during a crawler fetch pins the repo to "no preview" for the
    whole cache TTL — the replaceable-doc empty-read shape, in preview form."""
    import asyncio
    from app.services import git_share, nostr_store
    git_share._cache.clear()
    calls = {"n": 0}

    async def _dead(port, filters, timeout=6.0, *, strict=False):
        calls["n"] += 1
        assert strict, "the read must be strict, or a dead relay looks like an empty one"
        raise RuntimeError("relay unreachable")

    monkeypatch.setattr(nostr_store, "_ws_query", _dead)
    assert asyncio.run(git_share.repo_card(3052, "a" * 64, "demo")) is None
    assert asyncio.run(git_share.repo_card(3052, "a" * 64, "demo")) is None
    assert calls["n"] == 2, "a failed read was cached — the next request must try again"


def test_og_meta_always_has_a_title_and_a_description():
    """A card with an empty description renders as a bare URL, which is the state this whole feature
    exists to replace — so the fallback sentence is part of the contract, not a nicety."""
    from app.services import git_share
    m = git_share.og_meta({"name": "My App", "author": "alice", "description": "does things"},
                          "https://x/r/npub1/my-app")
    assert m["title"] == "My App · alice"
    assert m["description"] == "does things"
    bare = git_share.og_meta({"repo": "my-app"}, "https://x/r/npub1/my-app",
                             fallback_image="https://x/logo.png")
    assert bare["title"] == "my-app"
    assert bare["description"].strip()
    assert bare["image"] == "https://x/logo.png"


# ---------------------------------------------------------------- proxy allowlist


@pytest.mark.parametrize("path", [
    "npub1x/demo.git/info/refs",
    "npub1x/demo.git/git-upload-pack",
    "npub1x/demo.git/git-receive-pack",
    "npub1x/demo.git/refs",
    "npub1x/demo.git/tree/HEAD",
    "npub1x/demo.git/tree/HEAD/src",
    "npub1x/demo.git/log/HEAD",
    "npub1x/demo.git/raw/HEAD/README.md",
    "npub1x/demo.git/download/HEAD/README.md",
    "npub1x/demo.git/commit/abc123",
    "npub1x/demo.git/archive/HEAD.tar.gz",
    "npub1x/demo.git/edit",
    "npub1x/demo.git/create",
    "npub1x/demo.git/delete",
])
def test_proxy_forwards_every_route_the_host_serves(path, monkeypatch):
    """A proxy node that forwards a clone but 404s the browse API is a repo you can copy and cannot
    read — and it fails only on the proxy nodes, which is why the web UI (which reads through
    /client/git/*) never noticed. Each of these is read-gated on the HOSTING node exactly like a
    clone, so forwarding them grants nothing a clone did not."""
    import asyncio
    from app.services import git_proxy
    from fastapi import HTTPException
    monkeypatch.setattr(git_proxy, "_base_url", lambda: "http://127.0.0.1:1")

    class _Req:
        method = "GET"
        headers = {}

        class url:
            query = ""

        async def body(self):
            return b""

    # It must get PAST the allowlist. Everything downstream is a real socket to a dead port, so a
    # connection failure (503) means the path was accepted; a 404 means it was rejected here.
    try:
        asyncio.run(git_proxy.proxy_git_request(_Req(), path))
    except HTTPException as e:
        assert e.status_code != 404, "%s was rejected by the allowlist" % path
    except Exception:
        pass


@pytest.mark.parametrize("path", [
    "example.com/anything",
    "npub1x/demo.git/../../etc/passwd",
    "npub1x/demo.git",
    "",
])
def test_proxy_is_still_not_an_open_proxy(path, monkeypatch):
    import asyncio
    from app.services import git_proxy
    from fastapi import HTTPException
    monkeypatch.setattr(git_proxy, "_base_url", lambda: "http://127.0.0.1:1")

    class _Req:
        method = "GET"
        headers = {}

        class url:
            query = ""

        async def body(self):
            return b""

    with pytest.raises(HTTPException) as ei:
        asyncio.run(git_proxy.proxy_git_request(_Req(), path))
    assert ei.value.status_code == 404, path


# ---------------------------------------------------------------- the shell's link preview


def _render_shell(meta):
    """Render the REAL client shell template. Asserting against the shipped file is the point: a
    preview that is correct in a helper and absent from <head> previews nothing."""
    from jinja2 import Environment, FileSystemLoader

    class _FakeReq:
        url = type("u", (), {"scheme": "https"})()
        headers = {}
        base_url = "https://example.test/"

    env = Environment(loader=FileSystemLoader(os.path.join(_ROOT, "templates")),
                      autoescape=True)
    return env.get_template("client.html").render(
        request=_FakeReq(), ver="1", build="abc", secure=True, nostr_only=False,
        default_theme="cyberpunk", meta=meta)


def test_shell_renders_a_real_card_when_a_route_supplies_one():
    html = _render_shell({"title": "PosterChanAI · alice", "description": "a self-hosted thing",
                          "url": "https://example.test/r/npub1x/posterchanai",
                          "image": "https://example.test/pic.png", "type": "object"})
    assert "<title>PosterChanAI · alice</title>" in html
    assert 'property="og:title" content="PosterChanAI · alice"' in html
    assert 'property="og:description" content="a self-hosted thing"' in html
    assert 'property="og:url" content="https://example.test/r/npub1x/posterchanai"' in html
    assert 'property="og:image" content="https://example.test/pic.png"' in html
    # A card WITH a picture must claim the large-image layout, or the picture is fetched and shown
    # as a thumbnail the size of a favicon.
    assert 'name="twitter:card" content="summary_large_image"' in html


def test_shell_omits_the_image_tag_entirely_when_there_is_none():
    """An `og:image` with an empty content makes some crawlers render a blank box instead of falling
    back to the text card — worse than having no picture."""
    html = _render_shell({"title": "x", "description": "y", "url": "", "image": "", "type": "object"})
    # The TAG, not the word: the template's own comment explains this rule and says "og:image" while
    # doing so, so a substring check passes and fails for reasons that have nothing to do with <head>.
    assert 'property="og:image"' not in html
    assert 'name="twitter:image"' not in html
    assert 'name="twitter:card" content="summary"' in html


def test_every_other_route_is_unchanged():
    html = _render_shell(None)
    assert "<title>PosterChan · Nostr</title>" in html
    assert 'property="og:title"' not in html
    assert 'property="og:description"' not in html


def test_card_text_is_escaped_into_the_meta_tag():
    """Repo names and descriptions are attacker-controlled — anyone can publish a 30617. A quote in
    one must not close the content attribute."""
    html = _render_shell({"title": 'evil" onload="alert(1)', "description": "<script>x</script>",
                          "url": "", "image": "", "type": "object"})
    assert 'onload="alert(1)' not in html
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html
