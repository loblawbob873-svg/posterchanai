"""ONE number, and it limits the INTERNET -- the shape Jellyfin ships, for the same reason.

The reported fault, measured live on 2026-09-17 while it was happening: a TV at 192.168.0.49 (router
nginx -> server1 -> nas) paced to `viewer_kbps` 1600, so 480p segments took 6-13 s to deliver ~600-690
KB with headers in 0.08 s -- a 6-second segment arriving slower than real time
(`[media-proxy] ... elapsed_s=13.907 upstream_bytes=682064`). The buffer drained, the player fell
480p -> 240p, and at the rung switch the Jellyfin client stopped and restarted the stream.

The rule this file pins: `viewer_kbps` is the per-viewer ceiling on this node's UPLOAD, so it is
charged to viewers who use the uplink and to nobody else. `server_kbps`, the total, still applies to
everyone. A SECOND setting was tried first and removed: it asked somebody to describe one intention
twice, which no other media server does.

Who counts as "not the internet" is the strict part -- this node's OWN attached subnets, never a VPN
peer (measured: WireGuard hands roaming phones 192.168.5.2-5 and the VPS 192.168.7.1, all RFC1918,
all across the WAN), never `ip.is_private`, and never a viewer this node could not measure.

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


def test_an_unreadable_interface_table_charges_everybody(monkeypatch):
    """Failing to measure means the viewer is treated as the internet -- paced, which is the
    pre-existing behaviour and a bounded, harmless loss -- and never the other way round. A STALE
    answer must not survive the failure either, which is the shape a cached measurement invites."""
    import ipaddress

    def explode():
        raise OSError("no interface table")

    monkeypatch.setitem(__import__("sys").modules, "psutil", SimpleNamespace(net_if_addrs=explode))
    # A cache holding a real answer, then the table becomes unreadable.
    monkeypatch.setattr(media, "_local_networks", (0.0, (ipaddress.ip_network("192.168.0.0/24"),)))
    assert media.local_networks() == ()
    assert media.is_local_address("192.168.0.49") is False
    assert media.metered(media.is_local_address("192.168.0.49")) is True
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


def test_the_internet_is_charged_and_this_network_is_not(api, monkeypatch):  # noqa: F811
    """One number. A remote viewer is paced to it; a viewer on this node's own network is not
    charged against it at all -- and the SERVER total still applies to both."""
    client, docs, user, folder = api
    seed(docs, folder)
    calls, limits = [], dict(media.DEFAULT_LIMITS, viewer_kbps=800)

    async def encoded(*args):
        return b"segment-bytes"

    async def spy(data, viewer, config, order=None, metered=True):
        calls.append({"metered": metered, "viewer_kbps": config["viewer_kbps"],
                      "server_kbps": config["server_kbps"]})
        yield data

    async def configured():
        return dict(limits)

    monkeypatch.setattr(media, "segment", encoded)
    monkeypatch.setattr(media, "prefetch", lambda *args: None)
    monkeypatch.setattr(media, "paced_bytes", spy)
    monkeypatch.setattr(media, "limits", configured)
    url = client.post("/api/media-center/abc/play/movie").json()["url"].replace("master.m3u8", "240p-1.ts")

    monkeypatch.setattr(routes, "client_address", lambda request: "69.145.1.133")
    assert client.get(url).content == b"segment-bytes"
    assert calls[-1]["metered"] is True, "a viewer on the internet must be charged the cap"

    monkeypatch.setattr(routes, "client_address", lambda request: "192.168.0.49")
    assert client.get(url).content == b"segment-bytes"
    assert calls[-1]["metered"] is False, "a viewer on this network does not use the uplink"
    # The number itself is never rewritten, and the server-wide total reaches the pacer either way.
    assert {call["viewer_kbps"] for call in calls} == {800}
    assert {call["server_kbps"] for call in calls} == {media.DEFAULT_LIMITS["server_kbps"]}


def test_the_server_total_still_applies_to_a_viewer_on_this_network():
    """The per-viewer cap is about the uplink; the total is about the machine, and everybody pays it.
    Dropping BOTH budgets would let one local player take the whole server."""
    import inspect
    source = inspect.getsource(media.paced_bytes)
    unmetered = source[source.index("if metered else"):]
    assert '"server"' in unmetered.split(")")[0] + ")", unmetered[:120]


def test_the_ladder_follows_the_same_rule_as_the_rate(api, monkeypatch):  # noqa: F811
    """A player offered a rung its budget cannot carry stalls at that rung, so the master playlist
    and the pacing must agree about who is charged."""
    client, docs, user, folder = api
    seed(docs, folder)
    limits = dict(media.DEFAULT_LIMITS, viewer_kbps=1600)

    async def configured():
        return dict(limits)

    monkeypatch.setattr(media, "limits", configured)
    url = client.post("/api/media-center/abc/play/movie").json()["url"]

    monkeypatch.setattr(routes, "client_address", lambda request: "69.145.1.133")
    assert "720p.m3u8" not in client.get(url).text, "1600 kbps of uplink cannot carry 720p"
    assert "720p" not in client.get("/api/media-center").json()["profiles"]

    monkeypatch.setattr(routes, "client_address", lambda request: "192.168.0.49")
    assert "720p.m3u8" in client.get(url).text, "a viewer on this network is not held to the uplink"
    # The quality menu is built from this list, so it must answer for the same viewer.
    assert "720p" in client.get("/api/media-center").json()["profiles"]


def test_only_the_internet_is_metered():
    assert media.metered(False) is True          # not local -> the internet -> charged
    assert media.metered(True) is False          # this node's own network -> not charged


def test_there_is_exactly_one_bandwidth_number_per_viewer():
    """A second setting was tried and removed. Two boxes asked somebody to describe one intention
    twice; the fix was to name the one number after what it limits."""
    assert "lan_kbps" not in media.DEFAULT_LIMITS
    assert "lan_kbps" not in routes.Limits.model_fields
    source = (routes.__file__.rsplit("/app/", 1)[0] + "/static/js/client/app.js")
    form = open(source, encoding="utf-8").read()
    form = form[form.index('<form id="mc-limits"'):]
    form = form[:form.index('</form>')]
    assert 'name="lan_kbps"' not in form
    # And the one that remains says what it limits, on screen.
    assert "Internet bandwidth per user" in form
    assert "own network is not limited" in form


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
