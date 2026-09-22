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
