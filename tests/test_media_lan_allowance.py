"""`lan_kbps`: a SECOND cap the operator types for this node's own network. Nothing here is exempt.

The reported fault, measured live on 2026-09-17 while it was happening: a TV at 192.168.0.49 (router
nginx -> server1 -> nas) paced to `viewer_kbps` 1600, so 480p segments took 6-13 s to deliver ~600-690
KB with headers in 0.08 s -- a 6-second segment arriving slower than real time
(`[media-proxy] ... elapsed_s=13.907 upstream_bytes=682064`). The buffer drained, the player fell
480p -> 240p, and at the rung switch the Jellyfin client stopped and restarted the stream.

The rule this file pins, and the reason it is a setting and not a cleverness: A CAP THE OPERATOR
TYPED IS ENFORCED, ALWAYS, ON EVERYBODY. `lan_kbps` defaults to 0 = no separate allowance, i.e. this
node behaves exactly as it did. Set it, and only viewers inside this node's OWN attached subnets are
paced to that number instead -- never a VPN peer (measured: WireGuard hands roaming phones
192.168.5.2-5 and the VPS 192.168.7.1, all RFC1918, all across the WAN), never `ip.is_private`, and
never a viewer this node could not measure.

The address is decided by `media.viewer_address`, which trusts a forwarded header only from a local
peer and the node-to-node `X-PC-Media-Client` only on an authenticated hop.
"""
import ast
import re
from types import SimpleNamespace

import pytest

from app.routers import media_center as routes
from app.services import media_center as media
from tests.test_media_center import api, seed  # noqa: F401  (fixture reuse)


def _request(peer, headers=None, internal=False):
    scope_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    request = SimpleNamespace(client=SimpleNamespace(host=peer) if peer else None,
                             headers={k.lower(): v for k, v in (headers or {}).items()},
                             scope={"headers": scope_headers})
    return request


@pytest.fixture
def attached(monkeypatch):
    """This node's real interface table on server1/nas: one LAN, loopback, a docker bridge."""
    import ipaddress
    networks = tuple(ipaddress.ip_network(cidr) for cidr in
                     ("192.168.0.0/24", "127.0.0.0/8", "::1/128", "fe80::/64", "172.17.0.0/16"))
    monkeypatch.setattr(media, "local_networks", lambda: networks)
    return networks


@pytest.mark.parametrize("value,local", [
    ("192.168.0.49", True), ("127.0.0.1", True), ("::1", True), ("::ffff:192.168.0.4", True),
    ("172.17.0.9", True),
    # On this node's LAN only. The rest are somebody else's network, however private they look --
    # 192.168.5.3 and 192.168.7.1 are THIS deployment's WireGuard peers, over the WAN.
    ("192.168.5.3", False), ("192.168.7.1", False), ("192.168.1.4", False), ("10.1.2.3", False),
    ("172.16.5.6", False), ("fd12::9", False),
    ("8.8.8.8", False), ("69.145.1.133", False), ("2606:4700::1111", False), ("100.64.7.9", False),
    # Every one of these is `is_private` on some CPython, and none of them is on this network.
    ("2002:4d90:1::1", False), ("198.18.0.7", False), ("203.0.113.5", False), ("", False),
    ("not-an-address", False), ("testclient", False),
])
def test_what_counts_as_the_same_network(attached, value, local):
    assert media.is_local_address(value) is local


def test_a_vpn_peer_is_not_on_the_lan_however_private_its_address_looks(monkeypatch):
    """The leak this rule exists to prevent. `wg show` on the router: roaming phones hold
    192.168.5.2-5 and the VPS holds 192.168.7.1 -- RFC1918 addresses whose every byte crosses the
    WAN twice. A tunnel is point-to-point, so it never contributes a network."""
    class Entry:
        def __init__(self, address, netmask, ptp=None):
            self.address, self.netmask, self.ptp = address, netmask, ptp

    fake = SimpleNamespace(net_if_addrs=lambda: {
        "lo": [Entry("127.0.0.1", "255.0.0.0")],
        "enp12s0": [Entry("192.168.0.85", "255.255.255.0"),
                    Entry("fe80::7802:cac7:2fca:d0a3%enp12s0", "ffff:ffff:ffff:ffff::")],
        "home": [Entry("192.168.5.1", "255.255.255.255", ptp="192.168.5.2")],
        "cdn": [Entry("192.168.7.2", "255.255.255.0", ptp="192.168.7.1")],
    })
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake)
    monkeypatch.setattr(media, "_local_networks", (0.0, None))
    networks = [str(network) for network in media.local_networks()]
    assert "192.168.0.0/24" in networks and "127.0.0.0/8" in networks
    assert "192.168.7.0/24" not in networks, "a tunnel's subnet is not this node's network"
    assert media.is_local_address("192.168.0.49") is True
    for peer in ("192.168.5.2", "192.168.5.3", "192.168.7.1"):
        assert media.is_local_address(peer) is False, "a VPN peer was handed the uplink unpaced"
    monkeypatch.setattr(media, "_local_networks", (0.0, None))


def test_an_unreadable_interface_table_falls_back_to_the_general_cap(monkeypatch):
    """Failing to measure costs a viewer their LAN allowance -- the pre-existing behaviour, a bounded
    and harmless loss -- and never lets an unmeasured address keep one. A STALE answer must not
    survive the failure either, which is the shape a cached measurement invites."""
    import ipaddress

    def explode():
        raise OSError("no interface table")

    monkeypatch.setitem(__import__("sys").modules, "psutil", SimpleNamespace(net_if_addrs=explode))
    # A cache holding a real answer, then the table becomes unreadable.
    monkeypatch.setattr(media, "_local_networks", (0.0, (ipaddress.ip_network("192.168.0.0/24"),)))
    assert media.local_networks() == ()
    assert media.is_local_address("192.168.0.49") is False
    assert media.effective_limits({"viewer_kbps": 800, "lan_kbps": 40000},
                                  media.is_local_address("192.168.0.49"))["viewer_kbps"] == 800
    monkeypatch.setattr(media, "_local_networks", (0.0, None))


def test_a_forwarded_address_is_trusted_only_from_a_local_peer_or_a_signed_hop():
    # The router's nginx overwrites X-Real-IP with the address it saw, and it is itself local.
    assert media.viewer_address("192.168.0.1", real_ip="192.168.0.49") == "192.168.0.49"
    assert media.viewer_address("192.168.0.1", forwarded="192.168.0.49, 10.0.0.1") == "192.168.0.49"
    # From the open internet the headers are whatever the client typed.
    assert media.viewer_address("69.145.1.133", real_ip="192.168.0.49") == "69.145.1.133"
    assert media.viewer_address("69.145.1.133", forwarded="10.0.0.5") == "69.145.1.133"
    # The edge node's measurement crosses the hop only when the hop is authenticated.
    assert media.viewer_address("69.145.1.133", relayed="192.168.0.49", internal=True) == "192.168.0.49"
    assert media.viewer_address("69.145.1.133", relayed="192.168.0.49", internal=False) == "69.145.1.133"


def test_a_node_hop_that_says_nothing_is_paced_not_exempted():
    """Every remote viewer reaches the NAS over the SAME hop as the TV: the peer is server1, which is
    local. Falling back to the peer there would exempt the whole internet from pacing the moment an
    edge stopped sending its measurement."""
    assert media.viewer_address("192.168.0.2", relayed="", internal=True) == ""
    assert media.is_local_address(media.viewer_address("192.168.0.2", relayed="", internal=True)) is False
    assert media.viewer_address("192.168.0.2", relayed="nonsense", internal=True) == ""
    # A forwarded header on the hop is the client's own text; only the signed measurement counts.
    assert media.viewer_address("192.168.0.2", real_ip="192.168.0.49", internal=True) == ""
    assert media.viewer_address("192.168.0.2", relayed="192.168.0.49", internal=True) == "192.168.0.49"


def test_the_edge_reads_the_address_the_same_way():
    assert routes.client_address(_request("192.168.0.1", {"X-Real-IP": "192.168.0.49"})) == "192.168.0.49"
    assert routes.client_address(_request("69.145.1.133", {"X-Real-IP": "192.168.0.49"})) == "69.145.1.133"
    assert routes.client_address(_request("")) == ""


def test_the_typed_cap_applies_to_everybody_until_a_lan_number_is_typed(api, monkeypatch):  # noqa: F811
    """Nobody is ever served unpaced, and no cap is ever raised by this code. With `lan_kbps` unset
    a 100 KB/s cap is 100 KB/s for the TV too -- exactly what a node does today."""
    client, docs, user, folder = api
    seed(docs, folder)
    rates, limits = [], dict(media.DEFAULT_LIMITS, viewer_kbps=800)

    async def encoded(*args):
        return b"segment-bytes"

    async def spy(data, viewer, config, order=None):
        rates.append(config["viewer_kbps"])
        yield data

    async def configured():
        return dict(limits)

    monkeypatch.setattr(media, "segment", encoded)
    monkeypatch.setattr(media, "prefetch", lambda *args: None)
    monkeypatch.setattr(media, "paced_bytes", spy)
    monkeypatch.setattr(media, "limits", configured)
    url = client.post("/api/media-center/abc/play/movie").json()["url"].replace("master.m3u8", "240p-1.ts")

    monkeypatch.setattr(routes, "client_address", lambda request: "192.168.0.49")
    assert client.get(url).content == b"segment-bytes"
    assert rates == [800], "an unset lan_kbps must leave the operator's cap applying to everyone"

    limits["lan_kbps"] = 40000
    assert client.get(url).content == b"segment-bytes"
    assert rates[-1] == 40000, "a typed LAN allowance was ignored"

    monkeypatch.setattr(routes, "client_address", lambda request: "69.145.1.133")
    assert client.get(url).content == b"segment-bytes"
    assert rates[-1] == 800, "a LAN allowance must never reach a viewer off this network"


def test_the_ladder_and_the_rate_come_from_the_same_limits(api, monkeypatch):  # noqa: F811
    """A player offered a rung its budget cannot carry stalls at that rung. The master playlist and
    the pacing must therefore read ONE config, so a LAN allowance opens the ladder it pays for."""
    client, docs, user, folder = api
    seed(docs, folder)
    limits = dict(media.DEFAULT_LIMITS, viewer_kbps=1600)

    async def configured():
        return dict(limits)

    monkeypatch.setattr(media, "limits", configured)
    monkeypatch.setattr(routes, "client_address", lambda request: "192.168.0.49")
    url = client.post("/api/media-center/abc/play/movie").json()["url"]
    assert "720p.m3u8" not in client.get(url).text, "1600 kbps cannot carry 720p"

    limits["lan_kbps"] = 40000
    assert "720p.m3u8" in client.get(url).text, "a LAN allowance bought no better picture"

    # The quality menu is built from this list, so it must answer for the same viewer.
    assert "720p" in client.get("/api/media-center").json()["profiles"]

    monkeypatch.setattr(routes, "client_address", lambda request: "69.145.1.133")
    assert "720p.m3u8" not in client.get(url).text, "a remote viewer was offered the LAN ladder"
    assert "720p" not in client.get("/api/media-center").json()["profiles"]


def test_effective_limits_never_raises_a_number_nobody_typed():
    base = {"viewer_kbps": 800, "lan_kbps": 0, "server_kbps": 20000}
    assert media.effective_limits(base, True) == base
    assert media.effective_limits(base, False) == base
    assert media.effective_limits({**base, "lan_kbps": 40000}, False)["viewer_kbps"] == 800
    assert media.effective_limits({**base, "lan_kbps": 40000}, True)["viewer_kbps"] == 40000
    # The server-wide ceiling is never touched: it is the uplink itself.
    assert media.effective_limits({**base, "lan_kbps": 40000}, True)["server_kbps"] == 20000
    # A LAN allowance SMALLER than the general cap is still obeyed -- it is a number, not a bonus.
    assert media.effective_limits({**base, "lan_kbps": 300}, True)["viewer_kbps"] == 300


def test_the_jellyfin_hop_carries_the_clients_address():
    """`media_call` builds its own ASGI scope; without these headers every Jellyfin viewer looks like
    the router, and the router is local."""
    source = (routes.__file__.rsplit("/", 2)[0] + "/routers/jellyfin.py")
    text = open(source, encoding="utf-8").read()
    tree = ast.parse(text)
    call = next(node for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "media_call")
    body = ast.get_source_segment(text, call)
    assert "x-real-ip" in body and "x-forwarded-for" in body
    proxy = (routes.__file__)
    text = open(proxy, encoding="utf-8").read()
    assert 'headers["X-PC-Media-Client"] = client_address(request)' in text, \
        "the edge never tells the NAS who the viewer is"


def test_a_slow_segment_says_whether_that_viewer_was_paced():
    """Three rounds of this report were spent guessing who the viewer was, because the one line the
    proxy writes about a slow segment never said. It records the VERDICT, not the address."""
    text = open(routes.__file__, encoding="utf-8").read()
    record = text[text.index("def _log_segment_delivery("):text.index("async def proxy_request(")]
    assert "local=%s" in record
    assert "viewer_local = media.is_local_address(" in text
    calls = re.findall(r"(?<!def )_log_segment_delivery\((.*?)\)", text, re.S)
    assert len(calls) >= 3, "the proxy logs a segment from more than one place"
    for call in calls:
        assert "viewer_local" in call, "a diagnostic that omits the verdict is the report we already had"


def test_every_limit_has_a_box_to_type_it_in():
    """A limit with no input never hydrates and never saves: the operator types a number, Save posts
    nothing for it, and the node quietly keeps the old one. The form is read generically by name."""
    source = routes.__file__.rsplit("/app/", 1)[0] + "/static/js/client/app.js"
    form = open(source, encoding="utf-8").read()
    form = form[form.index('<form id="mc-limits"'):]
    form = form[:form.index('</form>')]
    for field in routes.Limits.model_fields:
        assert f'name="{field}"' in form, f"{field} cannot be typed in Bandwidth & resources"
        assert field in media.DEFAULT_LIMITS, f"{field} has no default on the server"
    for field in media.DEFAULT_LIMITS:
        assert field in routes.Limits.model_fields, f"{field} cannot be saved"


def test_the_jellyfin_shortcut_calls_every_route_with_the_arguments_it_declares():
    """`media_call` invokes the media routes IN PROCESS and POSITIONALLY, so a route's parameter
    ORDER is an API those calls depend on. Adding `request` in front of `user` silently handed a User
    object to the request slot -- every Jellyfin library listing 500'd, and only a full-suite run
    said so. Each positional argument must land on a parameter whose name it matches."""
    import inspect

    source = routes.__file__.rsplit("/", 1)[0] + "/jellyfin.py"
    tree = ast.parse(open(source, encoding="utf-8").read())
    checked = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "native"):
            continue
        route = getattr(routes, node.func.attr, None)
        if not callable(route):
            continue
        names = list(inspect.signature(route).parameters)
        for position, argument in enumerate(node.args):
            expected = {"auth.user": "user", "request": "request", "db": "db"}.get(ast.unparse(argument))
            if expected is None:
                continue
            assert position < len(names), f"{node.func.attr} takes fewer arguments than it is given"
            assert names[position] == expected, (
                f"native.{node.func.attr} passes {ast.unparse(argument)} into '{names[position]}'")
            checked += 1
    assert checked >= 5, "the jellyfin shortcut should be calling several media routes"


def test_the_jellyfin_shortcut_says_who_is_asking():
    """A route that takes `request` decides this viewer's limits with it. Called in process without
    one, `request is None` reads as "not on this network" -- so the Jellyfin client, which is the
    client that reported the buffering, could never be recognised however the operator configured
    the node, and nothing anywhere would say so."""
    source = routes.__file__.rsplit("/", 1)[0] + "/jellyfin.py"
    tree = ast.parse(open(source, encoding="utf-8").read())
    import inspect

    asked = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "native"):
            continue
        route = getattr(routes, node.func.attr, None)
        if not callable(route) or "request" not in inspect.signature(route).parameters:
            continue
        names = list(inspect.signature(route).parameters)
        filled = (len(node.args) > names.index("request")
                  or any(keyword.arg == "request" for keyword in node.keywords))
        assert filled, f"native.{node.func.attr} never says who is asking"
        asked += 1
    assert asked >= 2, "list_libraries and hls both take a request"
