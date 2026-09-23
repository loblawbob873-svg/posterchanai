"""PosterChan's own Tor publishes other local services as .onion (tor_extra_onions), so a node needs
no second, system Tor — nas.lan ran tor.service ONLY to host Akkoma's hidden service.

Every accepted line becomes torrc text, so the parser is the security boundary: a name is a directory
under the Tor data dir (no paths), the target must be this host, and nothing can smuggle a torrc line.
"""
import os
import stat

from app.services import tor_service as ts


def test_valid_lines_are_parsed():
    got = ts.parse_extra_onions("akkoma 80 127.0.0.1:8099\n# a comment\n\nwiki 8080 localhost:3000  # trailing\n")
    assert got == [("akkoma", 80, "127.0.0.1:8099"), ("wiki", 8080, "localhost:3000")]


def test_bad_lines_are_never_written():
    bad = "\n".join([
        "../etc 80 127.0.0.1:8099",            # a path, not a name
        "evil 80 10.0.0.5:80",                 # another machine through our onion
        "x 80 127.0.0.1:8099 SocksPort 0.0.0.0:9050",   # smuggled torrc
        "big 70000 127.0.0.1:80",              # port out of range
        "Upper 80 127.0.0.1:80",               # names are lower-case
        "akkoma 80 127.0.0.1:8099",
        "akkoma 81 127.0.0.1:8100",            # duplicate name
    ])
    assert ts.parse_extra_onions(bad) == [("akkoma", 80, "127.0.0.1:8099")]


def test_torrc_carries_each_service_in_a_private_dir(tmp_path):
    t = ts.TorService(data_dir=str(tmp_path / "tor"), extra_onions="akkoma 80 127.0.0.1:8099")
    rc = t._create_torrc().read_text()
    d = tmp_path / "tor" / "onion_extra" / "akkoma"
    assert f"HiddenServiceDir {d}\nHiddenServiceVersion 3\nHiddenServicePort 80 127.0.0.1:8099" in rc
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, "tor refuses a hidden-service dir others can read"
    assert "SocksPort 0.0.0.0" not in rc


def test_it_is_an_admin_setting_that_reaches_start_from_settings():
    from app.schemas import SettingsResponse
    assert "tor_extra_onions" in SettingsResponse.model_fields
    src = open(ts.__file__).read()
    assert 'extra_onions=_ss.get("tor_extra_onions", "")' in src
    html = open(os.path.join(os.path.dirname(ts.__file__), "..", "..", "templates", "admin", "tabs", "network.html")).read()
    assert 'id="tor_extra_onions" name="tor_extra_onions"' in html


# ---- the one that was missing, and what it cost -------------------------------------------------

def test_start_from_settings_actually_starts(monkeypatch, tmp_path):
    """RUN the call site. This feature shipped broken and no test noticed.

    `start_from_settings` passed `extra_onions=` to `start_tor_service`, which had no such
    parameter — the class took one, the factory did not. So the tor role died with
    `start_tor_service() got an unexpected keyword argument 'extra_onions'` on the first node that
    actually filled the setting in, taking every .onion and the whole Tor proxy down with it. The
    test that was supposed to cover this asserted the CALL TEXT was present in the source
    (`'extra_onions=_ss.get(...)' in src`), which is true of a call that cannot execute.

    So this one calls it, with the launch stubbed: whether the daemon comes up needs a real Tor,
    but whether the code can be CALLED AT ALL does not, and that is the half that broke.
    """
    started = {}

    def fake_start(self):
        started["service"] = self
        return True

    monkeypatch.setattr(ts.TorService, "start", fake_start)
    monkeypatch.setattr(ts, "_services", [])
    monkeypatch.setattr(ts.TorService, "_instance", None)
    values = {
        "tor_enabled": "true", "tor2_enabled": "false",
        "tor_data_dir": str(tmp_path / "tor"),
        "tor_extra_onions": "akkoma 80 127.0.0.1:8099",
    }
    from app.services import settings_store as store
    monkeypatch.setattr(store, "get", lambda k, d=None: values.get(k, d))
    monkeypatch.setattr(store, "get_bool", lambda k, d=False: str(values.get(k, d)).lower() in ("1", "true", "yes", "on"))
    monkeypatch.setattr(store, "get_int", lambda k, d=0: int(values.get(k, d)))

    assert ts.start_from_settings() is True, "the tor role could not start"
    assert started["service"].extra_onions == [("akkoma", 80, "127.0.0.1:8099")], \
        "the setting never reached the daemon that publishes the onion"


def test_the_factory_takes_every_argument_the_call_site_gives_it():
    """The generic form of the bug above: a mismatch between these two is a TypeError at RUNTIME, in
    a background role, where the only symptom is that Tor is gone."""
    import ast
    import inspect
    takes = set(inspect.signature(ts.start_tor_service).parameters)
    tree = ast.parse(inspect.getsource(ts.start_from_settings))
    passed = {kw.arg for node in ast.walk(tree) if isinstance(node, ast.Call)
              and getattr(node.func, "id", "") == "start_tor_service"
              for kw in node.keywords if kw.arg}
    assert passed, "the call site moved — this guard is now watching nothing"
    assert passed <= takes, f"start_from_settings passes {sorted(passed - takes)}, which the factory has no parameter for"


def test_a_service_on_this_machines_lan_address_is_publishable():
    """Loopback was not enough, and the Akkoma cutover is why: Akkoma binds the machine's LAN
    address (192.168.0.85:4000 on nas), not 127.0.0.1, so a loopback-only rule refused to publish
    the node's OWN service while reporting the line as invalid.

    "This host" is answered by BINDING the address — a local syscall, no DNS, no network — so it
    cannot be talked into saying yes about another machine, which is the whole point of the guard:
    a hidden service forwarding somewhere else publishes that somewhere else under our identity."""
    import socket
    mine = socket.gethostbyname(socket.gethostname())
    assert ts.parse_extra_onions(f"akkoma 80 {mine}:4000") == [("akkoma", 80, f"{mine}:4000")]
    assert ts.parse_extra_onions("akkoma 80 127.0.0.1:8099") == [("akkoma", 80, "127.0.0.1:8099")]
    # Somebody else's machine, in three flavours: a public address, a LAN address that is not ours,
    # and a name. Each would be published through this node's onion identity.
    for bad in ("x 80 8.8.8.8:80", "x 80 203.0.113.7:443", "x 80 example.com:80"):
        assert ts.parse_extra_onions(bad) == [], bad


def test_a_name_or_link_local_target_is_refused_even_if_it_binds(monkeypatch):
    """bind() RESOLVES a hostname and Tor resolves it again, later and separately — so a name that
    answers 127.0.0.1 to the check (localtest.me, a hosts entry, a rebinding record) can point Tor
    anywhere. And a host with net.ipv4.ip_nonlocal_bind=1 (or an IPv6 AnyIP route) binds ANY
    address, so the bind alone would publish the cloud metadata service. Simulate that host."""
    import socket

    class AnyBind:
        def __init__(self, *a, **k): pass
        def bind(self, addr): return None
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(socket, "socket", AnyBind)
    for bad in ("x 80 169.254.169.254:80", "x 80 [fe80::1]:80", "x 80 [::ffff:169.254.169.254]:80",
                "x 80 224.0.0.1:80", "x 80 localtest.me:80", "x 80 my-nas.lan:4000"):
        assert ts.parse_extra_onions(bad) == [], bad
    # The shapes that ARE this host still pass on such a machine.
    assert ts.parse_extra_onions("a 80 127.0.0.1:1\nb 80 localhost:2\nc 80 [::1]:3") == [
        ("a", 80, "127.0.0.1:1"), ("b", 80, "localhost:2"), ("c", 80, "[::1]:3")]
