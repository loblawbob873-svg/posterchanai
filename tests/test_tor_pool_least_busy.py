"""MORE TOR DAEMONS, AND THE PROXY SENDS EACH CONNECTION TO THE LEAST-BUSY ONE.

One tor process moves every cell on one thread, so a node with one or two daemons tops out at one or
two cores however many it has. Measured on nas.lan 2026-10-10: tor2 at 100% of a core while tor1 idled
at 2% -- the proxy round-robined, which balances the NUMBER of connections, and a few heavy ones had
landed on tor2. These tests drive the REAL proxy (HttpToSocksProxy, its own listener, real CONNECTs)
against three fake SOCKS5 backends standing in for three tor daemons, and read which backend each
connection actually reached.

The first test is the one round-robin fails: after a heavy download on backend 0, round-robin still
sends every third new connection there."""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import http_proxy_service as hp  # noqa: E402
from app.services import tor_service  # noqa: E402

HEAVY = 4 * 1024 * 1024


class FakeTor:
    """A minimal SOCKS5 server: records each CONNECT's host, streams HEAVY bytes for host 'heavy',
    then holds the connection until the client closes it."""

    def __init__(self, refuse=False):
        self.hosts = []
        self.refuse = refuse
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _serve(self, r, w):
        try:
            await r.readexactly(3)
            w.write(b"\x05\x00")
            hdr = await r.readexactly(4)
            if hdr[3] == 3:
                host = (await r.readexactly((await r.readexactly(1))[0])).decode()
            else:
                host = "ip"
                await r.readexactly(4 if hdr[3] == 1 else 16)
            await r.readexactly(2)
            if self.refuse:
                w.write(b"\x05\x05\x00\x01" + bytes(6))
                await w.drain()
                return
            self.hosts.append(host)
            w.write(b"\x05\x00\x00\x01" + bytes(6))
            if host == "heavy":
                w.write(b"x" * HEAVY)
            await w.drain()
            while await r.read(65536):
                pass
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


async def _connect(port, host):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    await w.drain()
    status = await r.readline()
    while (await r.readline()) not in (b"\r\n", b""):
        pass
    return r, w, status


async def _scenario(body, refuse=()):
    fakes = [FakeTor(refuse=i in refuse) for i in range(3)]
    ports = [await f.start() for f in fakes]
    proxy = hp.HttpToSocksProxy(listen_port=0, socks_ports=[f"{p}:t{i}" for i, p in enumerate(ports)])
    await proxy._start_server()
    lport = proxy._server.sockets[0].getsockname()[1]
    try:
        return await body(lport, fakes, proxy)
    finally:
        proxy._server.close()
        for f in fakes:
            f.server.close()


async def _light(lport, n):
    """n short connections, each opened, answered and closed before the next."""
    for _ in range(n):
        r, w, status = await _connect(lport, "light")
        assert b"200" in status, status
        w.close()
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)


class LeastBusy(unittest.TestCase):
    def test_new_connections_avoid_the_backend_carrying_a_heavy_download(self):
        async def body(lport, fakes, proxy):
            r, w, status = await _connect(lport, "heavy")
            self.assertIn(b"200", status)
            got = 0
            while got < HEAVY:
                chunk = await r.read(65536)
                self.assertTrue(chunk, "the heavy download ended early")
                got += len(chunk)
            heavy_on = [i for i, f in enumerate(fakes) if "heavy" in f.hosts]
            await _light(lport, 6)
            w.close()
            return heavy_on, [f.hosts.count("light") for f in fakes]

        heavy_on, light = asyncio.run(_scenario(body))
        self.assertEqual(len(heavy_on), 1)
        self.assertEqual(light[heavy_on[0]], 0,
                         f"new connections went to the backend still busy with a 4 MB download: {light}")
        self.assertEqual(sum(light), 6)
        others = [n for i, n in enumerate(light) if i != heavy_on[0]]
        self.assertEqual(others, [3, 3], f"the idle backends should share the work evenly: {light}")

    def test_idle_backends_take_turns(self):
        async def body(lport, fakes, proxy):
            await _light(lport, 9)
            return [f.hosts.count("light") for f in fakes]

        self.assertEqual(asyncio.run(_scenario(body)), [3, 3, 3])

    def test_open_connections_count_and_are_released(self):
        async def body(lport, fakes, proxy):
            held = [await _connect(lport, "light") for _ in range(3)]
            await asyncio.sleep(0.05)
            spread = [f.hosts.count("light") for f in fakes]
            opened = list(proxy._open)
            for _, w, _ in held:
                w.close()
            await asyncio.sleep(0.2)
            return spread, opened, list(proxy._open), dict(proxy._lease)

        spread, opened, after, lease = asyncio.run(_scenario(body))
        self.assertEqual(spread, [1, 1, 1], "three held connections must land on three daemons")
        self.assertEqual(opened, [1, 1, 1])
        self.assertEqual(after, [0, 0, 0], "a closed tunnel must give its backend's slot back")
        self.assertEqual(lease, {})

    def test_a_refusing_backend_fails_over_and_tor_only_still_holds(self):
        async def body(lport, fakes, proxy):
            await _light(lport, 4)
            return [f.hosts.count("light") for f in fakes]

        light = asyncio.run(_scenario(body, refuse={0}))
        self.assertEqual(light[0], 0)
        self.assertEqual(sum(light), 4, "every connection must still be served by a working daemon")

        async def all_down(lport, fakes, proxy):
            return (await _connect(lport, "light"))[2]

        self.assertIn(b"502", asyncio.run(_scenario(all_down, refuse={0, 1, 2})),
                      "with every tor down the main listener must refuse, never go direct")


class _Settings:
    def __init__(self, **kv):
        self.kv = kv

    def get(self, k, d=None):
        return self.kv.get(k, d)

    def get_bool(self, k, d=False):
        v = self.kv.get(k)
        return d if v is None else str(v).lower() in ("1", "true", "yes", "on")

    def get_int(self, k, d=0):
        v = self.kv.get(k)
        return d if v in (None, "") else int(v)


class PoolPlan(unittest.TestCase):
    def test_auto_uses_more_daemons_on_a_bigger_box_and_none_on_a_small_one(self):
        plan = tor_service.pool_plan(_Settings(), cpus=12)
        self.assertEqual([(d["socks"], d["control"], d["dns"], d["label"]) for d in plan],
                         [(9072, 9073, 9075, "us3"), (9082, 9083, 9085, "ca4")])
        self.assertEqual([d["exits"] for d in plan], ["{us}", "{ca}"])
        self.assertTrue(plan[0]["data_dir"].endswith("/tor3"))
        self.assertEqual(tor_service.pool_plan(_Settings(), cpus=4), [])
        self.assertEqual(len(tor_service.pool_plan(_Settings(), cpus=64)), 4, "auto caps at 6 in total")
        self.assertEqual(len(tor_service.pool_plan(_Settings(tor2_enabled="false"), cpus=12)), 3)

    def test_a_number_is_honoured_and_capped(self):
        self.assertEqual(len(tor_service.pool_plan(_Settings(tor_extra_daemons="3"), cpus=2)), 3)
        self.assertEqual(len(tor_service.pool_plan(_Settings(tor_extra_daemons="0"), cpus=64)), 0)
        self.assertEqual(len(tor_service.pool_plan(_Settings(tor_extra_daemons="50"), cpus=2)),
                         tor_service.POOL_MAX_EXTRA)
        self.assertEqual(len(tor_service.pool_plan(_Settings(tor_extra_daemons="garbage"), cpus=12)), 2)

    def test_the_tor_role_starts_exactly_the_backends_the_proxy_balances_onto(self):
        from app.services import settings_store as ss
        fake = _Settings()
        with mock.patch.object(ss, "get", side_effect=fake.get), \
             mock.patch.object(ss, "get_bool", side_effect=fake.get_bool), \
             mock.patch.object(ss, "get_int", side_effect=fake.get_int), \
             mock.patch.object(tor_service.os, "cpu_count", return_value=12), \
             mock.patch.object(tor_service, "start_tor_service", return_value=object()) as start, \
             mock.patch.object(hp, "start_http_proxy_process") as proxy_start:
            self.assertTrue(tor_service.start_from_settings())
            self.assertTrue(hp.start_from_settings())
        started = [c.kwargs["socks_port"] for c in start.call_args_list]
        balanced = [int(s.split(":")[0]) for s in proxy_start.call_args.kwargs["socks_ports"]]
        self.assertEqual(started, [9052, 9062, 9072, 9082])
        self.assertEqual(balanced, started)


if __name__ == "__main__":
    unittest.main()
