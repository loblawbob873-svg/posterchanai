"""“72 people are now on the relay?” — the relay's people figure, and how it is checked.

Run: venv-unified/bin/python -m pytest tests/test_relay_people_count.py -q

The number the sidebar shows as “N on relay” (and Server Stats as “people connected”) is
`RelayServer.online_count()`: distinct client IPs among live websockets. It had **no test at all**,
which is how it managed to be wrong in both directions without anything on screen to say so:

* UNDER: until the router trusted the Cloudflare tunnel's own source address, nginx ignored
  `CF-Connecting-IP` and shipped `X-Real-IP: <our WAN address>` to the relay for every external
  client. The whole internet deduped to ONE address and the relay reported **7 people against 293
  open sockets**. Nothing looked broken — 7 is what a quiet relay looks like — and the only way to
  see it was to put the socket count next to it.
* OVER: our own machines (this node's LAN address, another node, the proxy) hold sockets here and
  are not people. They stay IN the total on purpose — on a LAN-only instance every real person is a
  private address, and subtracting them would report 0 people to a house full of them — so the
  breakdown has to say how many of the total they are instead.

So what is pinned here is not a number, it is that the figure is DECOMPOSABLE: every socket lands in
exactly one bucket and the buckets add up to what the surfaces print.
"""
import asyncio
import json

from app.services.nostr_relay.server import RelayServer


def _srv(ips):
    """A relay server holding `ips` — one entry per live connection, keyed by a fake conn."""
    srv = RelayServer.__new__(RelayServer)          # __init__ opens a store; we only need the tally
    srv._conn_ips = {("conn", i): ip for i, ip in enumerate(ips)}
    srv._conns = len(ips)
    return srv


# --- the headline figure ------------------------------------------------------------------------

def test_one_person_with_several_sockets_is_one_person():
    """Tabs, the PWA, a signer session and a reconnect all share an address. The count exists to
    collapse them — a raw socket count is why the figure was never believable."""
    srv = _srv(["41.90.1.2", "41.90.1.2", "41.90.1.2", "8.8.8.8"])
    assert srv.online_count() == 2
    b = srv.online_breakdown()
    assert (b["remote"], b["conns"]) == (2, 4)


def test_loopback_is_our_own_machinery_and_is_never_a_person():
    """The bots, the app and the worker dial ws://127.0.0.1:3052. They must not appear as visitors,
    and the sockets must still be accounted for, or the two numbers stop adding up."""
    srv = _srv(["127.0.0.1", "::1", "127.0.0.1", "41.90.1.2"])
    b = srv.online_breakdown()
    assert b["online"] == 1
    assert (b["loopback_conns"], b["conns"]) == (3, 4)


def test_an_address_we_could_not_read_counts_on_its_own():
    """"" means the extraction failed, NOT "the same unknown person": folding them together would
    report one visitor for any number of clients we cannot identify."""
    b = _srv(["", "", "41.90.1.2"]).online_breakdown()
    assert (b["online"], b["unknown"], b["remote"]) == (3, 2, 1)


def test_no_addresses_at_all_falls_back_to_sockets_and_says_so():
    """A relay that captured no IPs answers with the raw socket count — but flagged, because
    "8 people" and "8 sockets we could not attribute" are different claims."""
    srv = RelayServer.__new__(RelayServer)
    srv._conn_ips, srv._conns = {}, 8
    b = srv.online_breakdown()
    assert (b["online"], b["conns"], b["measured"]) == (8, 8, False)
    assert srv.online_count() == 8


def test_the_headline_is_exactly_the_breakdown_total():
    """Two code paths for one number is how a panel ends up disagreeing with a tooltip."""
    for ips in (["41.90.1.2", "41.90.1.2", "", "127.0.0.1", "192.168.0.2"], [], ["8.8.8.8"]):
        srv = _srv(ips)
        assert srv.online_count() == srv.online_breakdown()["online"]


# --- our own machines ---------------------------------------------------------------------------

def test_our_own_lan_machines_are_counted_but_reported_separately():
    """This node's own LAN address, a second node and the proxy are three sockets from three
    machines, not three visitors. They stay in the total (a LAN-only instance has no other kind of
    client), so the ONLY thing that can make the figure honest is saying how many there are."""
    b = _srv(["192.168.0.2", "192.168.0.85", "192.168.0.1", "41.90.1.2", "8.8.8.8"]).online_breakdown()
    assert b["internal"] == 3
    assert b["remote"] == 2
    assert b["online"] == 5                      # still counted — see the docstring
    assert b["online"] - b["internal"] == 2      # …and the operator can subtract them


def test_a_lan_only_instance_is_never_reported_as_empty():
    """The regression that a naive fix would ship: turnkey/home installs where every real person is
    a private address must not read "0 people" while the relay is serving them."""
    b = _srv(["192.168.1.10", "192.168.1.11", "10.0.0.5"]).online_breakdown()
    assert b["online"] == 3


# --- the measurement that was actually broken ---------------------------------------------------

def test_a_proxy_that_folds_every_client_into_one_address_is_visible_as_such():
    """The 2026-09-04 incident, reproduced: nginx did not trust the Cloudflare tunnel's source
    address, so `CF-Connecting-IP` was ignored and every external client arrived as our own WAN
    address. The count itself cannot tell that from one very keen visitor — `conns` beside it can,
    and that pair is what every surface now prints."""
    folded = _srv(["69.145.1.133"] * 293)
    b = folded.online_breakdown()
    assert b["online"] == 1 and b["conns"] == 293    # 1 person, 293 sockets = not a quiet relay

    # The same 293 sockets once the proxy passes the real client addresses through.
    real = _srv([f"41.90.{i // 256}.{i % 256}" for i in range(293)])
    assert real.online_count() == 293


# --- the parts have to survive the trip to the surfaces ------------------------------------------

def test_relay_status_passes_the_breakdown_through(tmp_path, monkeypatch):
    """`relay_status()` copies an ALLOW-LIST of keys out of the status file, so a number the relay
    measures but nobody listed is dropped in silence — the panel then prints a confident 0 for it.
    Written against a status file rather than a live relay because that allow-list is the part that
    breaks."""
    from app.services.nostr_relay import thread as th

    status = tmp_path / "relay.status.json"
    status.write_text(json.dumps({
        "running": True, "members": 51263, "conns": 264, "online": 153,
        "online_remote": 150, "online_internal": 3, "online_unknown": 0,
        "online_loopback_conns": 30, "online_measured": True,
        "calls": 0, "pid": 1, "ts": 2 ** 31, "started": 1,
    }))
    monkeypatch.setattr(th, "_relay_db_path", lambda: str(tmp_path / "relay"))
    monkeypatch.setattr(th, "_relay_paths", lambda p: {"status": str(status), "control": str(tmp_path)})
    monkeypatch.setattr(th, "_pid_alive", lambda pid: True)

    st = th.relay_status()
    assert (st["online"], st["conns"]) == (153, 264)
    assert (st["online_internal"], st["online_remote"]) == (3, 150)
    assert st["online_measured"] is True


def test_client_stats_ships_the_socket_count_beside_the_people_count(monkeypatch):
    """The sidebar chip reads /client/stats. It can only ever be as checkable as that payload, so
    the raw socket count and our own machines travel with the figure they explain."""
    from app.routers import client as cl
    from app.services.nostr_relay import thread as th

    monkeypatch.setattr(th, "relay_status", lambda: {
        "running": True, "members": 51263, "conns": 264, "online": 153,
        "online_internal": 3, "calls": 0})
    monkeypatch.setattr(cl, "_live_stream_count", lambda: _zero())

    class _Req:
        headers = {"x-forwarded-for": "41.90.1.2"}
        client = type("c", (), {"host": "41.90.1.2"})()

    body = json.loads(bytes(asyncio.run(cl.client_stats(_Req(), v="k" + "a" * 16)).body))
    assert body["relay"] == 153            # people (distinct client IPs)
    assert body["relay_sockets"] == 264    # what it was deduped from
    assert body["relay_internal"] == 3     # how many of the 153 are our own machines


async def _zero():
    return 0


# --- people, not sockets (2026-10-17: "make sure the number of users online is accurate") ---------

class _Conn:
    def __init__(self, here="192.168.0.2", confined=False):
        self.local_address = (here, 3052)
        self._pcai_signer_only = confined
        self._pcai_signer_used = False


class _Gate:
    def is_operator(self, pk):
        return pk == "op"


def _people(rows):
    """rows: (ip, conn, signed-in pubkeys)."""
    srv = RelayServer.__new__(RelayServer)
    srv._conn_ips = {c: ip for ip, c, _pks in rows}
    srv._auth_pubkeys = {c: set(pks) for _ip, c, pks in rows if pks}
    srv._conns = len(rows)
    srv.gate = _Gate()
    srv.cfg = {"node_pubkey": "op"}       # THIS node's key: the one signed-in key that is nobody
    return srv.online_breakdown()


def test_refused_strangers_and_our_own_processes_are_not_people():
    """110 'on relay' on server1: 68 sockets were confined strangers, and the app's own loopback sockets arrive
    through a NAT rule as the box's LAN address."""
    b = _people([("41.0.0.1", _Conn(), ()),                                # a guest
                 ("41.0.0.2", _Conn(confined=True), ()),                   # refused, redialling
                 ("41.0.0.3", _Conn(confined=True), ()),
                 ("192.168.0.2", _Conn(here="127.0.0.1"), ["op"])])        # this node, NAT'd
    assert b["online"] == 1, b
    assert (b["confined"], b["loopback_conns"], b["conns"]) == (2, 1, 4)


def test_one_signed_in_person_on_two_networks_is_one_and_a_shared_address_is_not_folded():
    b = _people([("41.0.0.1", _Conn(), ["alice"]),                         # phone
                 ("82.0.0.9", _Conn(), ["alice"]),                         # laptop
                 ("90.0.0.5", _Conn(), ["bob"]), ("90.0.0.5", _Conn(), ["carol"]),   # one household
                 ("90.0.0.5", _Conn(), ())])                               # carol's second tab, not signed in
    assert b["online"] == 3 and b["accounts"] == 3, b


def test_the_operator_key_is_not_a_person():
    b = _people([("41.0.0.1", _Conn(), ["op"])])
    assert b["online"] == 1 and b["accounts"] == 0, "counted once as an address, never as an account"


def test_a_client_signed_in_as_its_communities_stream_keys_is_still_one_person():
    """2026-10-27, "check our relay stats, no way 720 on it right now": 718 "online" over 430 sockets from 170 outside
    addresses. A client signs in as ITSELF and then as each Concord community's stream key it reads; every key was
    counted as a person. A socket is the key it signed in as first."""
    rows = []
    for i, who in enumerate(["alice", "bob"]):
        c = _Conn()
        c._pcai_first_auth = who
        rows.append(("41.0.0.%d" % i, c, [who] + ["stream%d" % k for k in range(10)]))   # ten communities each
    b = _people(rows)
    assert b["online"] == 2 and b["accounts"] == 2, b
