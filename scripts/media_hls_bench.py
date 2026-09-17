#!/usr/bin/env python3
"""Measure Media Center HLS the way a player experiences it, over a throttled link.

Two halves, run on two machines:

  serve  (on the media host)  the REAL Media Center router, transcoder, lookahead and byte pacer,
                              with an in-memory catalog holding ONE file and the viewer cap from
                              --viewer-kbps. It owns its own cache directory, touches no relay, no
                              database and no library folder (it never scans, so it never writes a
                              folder.png), and refuses any request without the --secret header.
  play   (anywhere)           an hls.js-shaped player: master + variant playlists, N parallel
                              segment fetches, EWMA bandwidth estimate (3s/9s half-lives, 500 kbps
                              default), 0.95/0.7 down/up factors, abandon-and-switch-down when a
                              load would stall, and cancel-on-switch like the Jellyfin client seen in
                              production. The access link is a token bucket shared by every
                              connection, with a small socket receive buffer so the server feels the
                              backpressure.

  play prints ONE JSON line: time to first frame, stalls (count and seconds), quality switches, bytes
  delivered, bytes wasted (cancelled or abandoned), and the time-weighted played bitrate and height.

Usage (the media host runs the code under test; any machine plays):

  media host:  PYTHONPATH=<tree> python scripts/media_hls_bench.py serve --file <video> \
                   --cache /tmp/mc-bench-cache --host <lan-ip> --port 38451 --secret <random> --encoder nvidia
  player:      python scripts/media_hls_bench.py play --base http://<lan-ip>:38451 \
                   --play-path <printed by serve> --secret <random> --link-kbps 600 --duration 150
               # the production Jellyfin client: --parallel 4 --meter aggregate --up-factor 0.8
               # hls.js (the web client):         --parallel 1 --meter per-load --cancel-on-switch 0

Clear the cache directory between runs, or the second run measures cache hits instead of encodes.

Not a check_*.py on purpose: it needs a media host and a real file, so it must not join ./test.sh.
"""
import argparse
import asyncio
import json
import math
import re
import os
import socket
import sys
import time
from urllib.parse import urljoin

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

HEADER = 'X-Bench-Secret'


# ─────────────────────────────── serve ───────────────────────────────

def serve(args):
    import copy
    from pathlib import Path
    from types import SimpleNamespace

    source = Path(args.file).resolve()
    os.environ['POSTERCHANAI_MEDIA_ROOTS'] = str(source.parent)
    os.environ['POSTERCHANAI_MEDIA_CACHE'] = args.cache
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from app.routers import media_center as routes
    from app.services import media_center as media

    stat = source.stat()
    item = {'id': 'b' * 32, 'name': source.stem, 'folder': '.', 'path': source.name,
            'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, **media.probe(source)}
    library = {'id': 'a' * 32, 'name': 'bench', 'folder': str(source.parent), 'owner': 'c' * 64,
               'shared_with': [], 'encoder': args.encoder, 'pages': ['page:bench'], 'count': 1}
    docs = {'index': {'ids': [library['id']]}, 'library:' + library['id']: library,
            'page:bench': [item],
            'limits': {**media.DEFAULT_LIMITS, 'viewer_kbps': args.viewer_kbps,
                       'server_kbps': args.server_kbps, 'max_transcodes': args.max_transcodes}}

    async def read(key):
        return copy.deepcopy(docs.get(key))

    async def write(key, value):
        docs[key] = copy.deepcopy(value)

    async def member(user):
        return user

    media.read, media.write = read, write
    routes.instance_membership.require_user = member
    user = SimpleNamespace(nostr_npub=library['owner'], is_admin=True, can_media=True)
    app = FastAPI()

    @app.middleware('http')
    async def gate(request, call_next):
        if request.headers.get(HEADER) != args.secret:
            return PlainTextResponse('no', status_code=403)
        return await call_next(request)

    app.include_router(routes.router)
    app.dependency_overrides[routes.media_user_optional] = lambda: user
    app.dependency_overrides[routes.get_db] = lambda: None
    print(json.dumps({'play': f"/api/media-center/{library['id']}/play/{item['id']}",
                      'duration': item['duration']}), flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level='warning')


# ─────────────────────────────── play ────────────────────────────────

class Link:
    """A shared access link: tokens in bytes, refilled at kbps, a burst of `burst` bytes."""

    def __init__(self, kbps, burst=16384):
        self.rate = kbps * 1000 / 8
        self.burst = burst
        self.tokens = burst
        self.stamp = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self, n):
        while n > 0:
            async with self.lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
                self.stamp = now
                grant = min(n, int(self.tokens))
                if grant > 0:
                    self.tokens -= grant
                    n -= grant
                    continue
                wait = (min(n, self.burst) - self.tokens) / self.rate
            await asyncio.sleep(max(wait, 0.002))


class Estimator:
    """hls.js EwmaBandWidthEstimator: fast/slow EWMAs weighted by load seconds, min of the two."""

    def __init__(self, default=500_000, fast=3.0, slow=9.0):
        self.default = default
        self.fast = [0.0, 0.0, math.exp(math.log(0.5) / fast)]
        self.slow = [0.0, 0.0, math.exp(math.log(0.5) / slow)]
        self.weight = 0.0

    def sample(self, seconds, bits):
        seconds = max(seconds, 0.05)
        bps = bits / seconds
        for ewma in (self.fast, self.slow):
            adj = math.pow(ewma[2], seconds)
            ewma[0] = bps * (1 - adj) + adj * ewma[0]
            ewma[1] += seconds
        self.weight += seconds

    def estimate(self):
        if self.weight < 0.1:
            return self.default
        vals = []
        for ewma in (self.fast, self.slow):
            zero = 1 - math.pow(ewma[2], ewma[1])
            vals.append(ewma[0] / zero if zero else ewma[0])
        return min(vals)


def parse_master(text, base):
    levels, bw = [], None
    for line in text.splitlines():
        if line.startswith('#EXT-X-STREAM-INF:'):
            bw = int(re.search(r'(?:^|[:,])BANDWIDTH=(\d+)', line).group(1))
        elif line and not line.startswith('#') and bw is not None:
            levels.append({'bandwidth': bw, 'uri': urljoin(base, line), 'name': line.split('.m3u8')[0]})
            bw = None
    return sorted(levels, key=lambda level: level['bandwidth'])


def parse_variant(text, base):
    segments, duration = [], None
    for line in text.splitlines():
        if line.startswith('#EXTINF:'):
            duration = float(line[8:].split(',')[0])
        elif line and not line.startswith('#'):
            segments.append((duration, urljoin(base, line)))
    return segments


HEIGHT = {'240p': 240, '360p': 360, '480p': 480, '720p': 720, '1080p': 1080}


async def play(args):
    import httpx
    link = Link(args.link_kbps)
    transport = httpx.AsyncHTTPTransport(
        socket_options=[(socket.SOL_SOCKET, socket.SO_RCVBUF, args.rcvbuf)])
    headers = {HEADER: args.secret}
    async with httpx.AsyncClient(transport=transport, headers=headers, timeout=60, base_url=args.base) as client:
        info = (await client.post(args.play_path)).json()
        master_url = urljoin(args.base, info['url'])
        levels = parse_master((await client.get(master_url)).text, master_url)
        for level in levels:
            level['segments'] = parse_variant((await client.get(level['uri'])).text, level['uri'])
        count = len(levels[0]['segments'])
        seg = levels[0]['segments'][0][0]
        start_index = min(count - 1, int(args.start / seg))
        est = Estimator()
        level = 0
        for i, candidate in enumerate(levels):   # hls.js startLevel from the default estimate
            if candidate['bandwidth'] <= est.estimate() * 0.95:
                level = i
        buffered = {}              # segment index -> level index
        inflight = {}              # segment index -> dict(task, level, bytes, started)
        stats = {'bytes': 0, 'wasted': 0, 'switches': 0, 'stalls': 0, 'stall_s': 0.0,
                 'ttff': None, 'played_bits': 0.0, 'played_height': 0.0, 'played_s': 0.0,
                 'abandons': 0, 'levels': [lv['name'] for lv in levels],
                 'bandwidths': [lv['bandwidth'] for lv in levels]}
        t0 = time.monotonic()
        playhead = start_index * seg
        playing, stall_at = False, None

        def buffer_end():
            i = math.floor(playhead / seg + 1e-9)
            while i in buffered:
                i += 1
            return i

        async def fetch(index, lvl):
            record = inflight[index]
            url = levels[lvl]['segments'][index][1]
            try:
                async with client.stream('GET', url) as response:
                    if response.status_code != 200:
                        await response.aread()
                        record['error'] = response.status_code
                        return
                    async for chunk in response.aiter_raw(4096):
                        await link.take(len(chunk))
                        record['bytes'] += len(chunk)
                record['done'] = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as error:   # a reset is a failed load, retried like a player would
                record['error'] = type(error).__name__

        def cancel(index, reason):
            record = inflight.pop(index)
            record['task'].cancel()
            stats['wasted'] += record['bytes']
            stats['bytes'] += record['bytes']
            if reason == 'abandon':
                stats['abandons'] += 1

        def switch(to):
            nonlocal level
            if to == level:
                return
            stats['switches'] += 1
            level = to
            if args.cancel_on_switch:
                for index in [i for i, r in inflight.items() if r['level'] != to]:
                    cancel(index, 'switch')

        meter = {'bytes': 0, 'active': 0.0}   # aggregate meter (ExoPlayer-style): all transfers together
        last = time.monotonic()
        end_media = min(count * seg, playhead + 10 ** 9)
        while time.monotonic() - t0 < args.duration and playhead < end_media - 0.01:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            dt, last = now - last, now
            if inflight:
                meter['active'] += dt
            # Completed loads enter the buffer and feed the estimator.
            for index, record in list(inflight.items()):
                if inflight.get(index) is not record:      # cancelled by a switch earlier in this pass
                    continue
                if record.get('error'):
                    inflight.pop(index)
                    stats['bytes'] += record['bytes']
                    stats['wasted'] += record['bytes']
                elif record.get('done'):
                    inflight.pop(index)
                    stats['bytes'] += record['bytes']
                    if args.meter == 'aggregate':
                        received = stats['bytes'] + sum(r['bytes'] for r in inflight.values())
                        est.sample(meter['active'], (received - meter['bytes']) * 8)
                        meter['bytes'], meter['active'] = received, 0.0
                    else:
                        est.sample(record['done'] - record['started'], record['bytes'] * 8)
                    if index >= math.floor(playhead / seg + 1e-9):
                        buffered[index] = record['level']
                    else:
                        stats['wasted'] += record['bytes']
                    ahead = buffer_end() * seg - playhead
                    best = 0
                    for i, candidate in enumerate(levels):
                        factor = args.up_factor if i > level else args.down_factor
                        if candidate['bandwidth'] <= est.estimate() * factor:
                            best = i
                    if best > level and ahead < args.min_up_buffer:
                        best = level
                    switch(best)
            # Playback clock.
            end = buffer_end() * seg
            if playing:
                stats['min_buffer'] = min(stats.get('min_buffer', 1e9), end - playhead)
            if playing:
                step = min(dt, max(0.0, end - playhead))
                if step > 0:
                    lvl = buffered.get(math.floor(playhead / seg + 1e-9), level)
                    stats['played_bits'] += levels[lvl]['bandwidth'] * step
                    stats['played_height'] += HEIGHT.get(levels[lvl]['name'], 0) * step
                    stats['played_s'] += step
                    playhead += step
                if playhead >= end - 1e-6 and playhead < end_media - 0.01:
                    playing, stall_at = False, now
                    stats['stalls'] += 1
            elif end - playhead >= min(seg, end_media - playhead) - 1e-6:
                playing = True
                if stats['ttff'] is None:
                    stats['ttff'] = now - t0
                elif stall_at is not None:
                    stats['stall_s'] += now - stall_at
                    stall_at = None
            # Abandon rule: a load projected to outlast the buffer switches down (hls.js).
            ahead = end - playhead
            # Only the load the buffer is waiting on can stall playback, so only it is judged (with one
            # load at a time, as in hls.js, that is the only load there is).
            gating = buffer_end()
            for index, record in list(inflight.items()):
                if index != gating:
                    continue
                elapsed = now - record['started']
                if record['level'] == 0 or elapsed < 0.5 or record.get('done') or record.get('error'):
                    continue
                duration = levels[record['level']]['segments'][index][0]
                expected = levels[record['level']]['bandwidth'] * duration / 8
                rate = record['bytes'] / elapsed
                if rate <= 0 or record['bytes'] >= expected:
                    continue
                remaining = (expected - record['bytes']) / rate
                if remaining > ahead + args.abandon_margin:
                    lower = next((i for i in range(record['level'] - 1, -1, -1)
                                  if levels[i]['bandwidth'] * duration / 8 / max(rate, 1) < ahead), 0)
                    cancel(index, 'abandon')
                    switch(min(level, lower))
            # Schedule loads: next missing segments, up to N in flight, within the buffer goal.
            index = buffer_end()
            while len(inflight) < args.parallel and index < count:
                if index in inflight or index in buffered:
                    index += 1
                    continue
                if index * seg - playhead >= args.buffer_goal:
                    break
                record = {'level': level, 'bytes': 0, 'started': time.monotonic()}
                inflight[index] = record
                record['task'] = asyncio.create_task(fetch(index, level))
                index += 1
        for index in list(inflight):
            cancel(index, 'end')
        if stall_at is not None:
            stats['stall_s'] += time.monotonic() - stall_at
        if stats['ttff'] is None:
            stats['ttff'] = float('inf')
        played = max(stats['played_s'], 1e-9)
        result = {'label': args.label, 'link_kbps': args.link_kbps, 'wall_s': round(time.monotonic() - t0, 1),
                  'ttff_s': round(stats['ttff'], 2), 'stalls': stats['stalls'],
                  'stall_s': round(stats['stall_s'], 1), 'switches': stats['switches'],
                  'abandons': stats['abandons'], 'played_s': round(stats['played_s'], 1),
                  'avg_bandwidth_kbps': round(stats['played_bits'] / played / 1000),
                  'avg_height': round(stats['played_height'] / played),
                  'bytes_mb': round(stats['bytes'] / 1e6, 2), 'wasted_mb': round(stats['wasted'] / 1e6, 2),
                  'kb_per_played_s': round(stats['bytes'] / 1000 / played, 1),
                  'min_buffer_s': round(stats.get('min_buffer', 0), 1),
                  'final_buffer_s': round(buffer_end() * seg - playhead, 1),
                  'ladder': dict(zip(stats['levels'], stats['bandwidths']))}
        print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('serve')
    s.add_argument('--file', required=True)
    s.add_argument('--cache', required=True, help='a directory under /tmp owned by this run')
    s.add_argument('--host', default='127.0.0.1')
    s.add_argument('--port', type=int, default=38451)
    s.add_argument('--secret', required=True)
    s.add_argument('--encoder', default='nvidia')
    s.add_argument('--viewer-kbps', type=int, default=1600)
    s.add_argument('--server-kbps', type=int, default=10000)
    s.add_argument('--max-transcodes', type=int, default=2)
    p = sub.add_parser('play')
    p.add_argument('--base', required=True)
    p.add_argument('--play-path', required=True)
    p.add_argument('--secret', required=True)
    p.add_argument('--link-kbps', type=int, required=True)
    p.add_argument('--duration', type=float, default=150)
    p.add_argument('--start', type=float, default=300)
    p.add_argument('--parallel', type=int, default=4)
    p.add_argument('--buffer-goal', type=float, default=30)
    p.add_argument('--up-factor', type=float, default=0.7)
    p.add_argument('--down-factor', type=float, default=0.95)
    p.add_argument('--min-up-buffer', type=float, default=0)
    p.add_argument('--abandon-margin', type=float, default=0)
    p.add_argument('--cancel-on-switch', type=int, default=1)
    p.add_argument('--meter', choices=['per-load', 'aggregate'], default='per-load')
    p.add_argument('--rcvbuf', type=int, default=32768)
    p.add_argument('--label', default='')
    args = parser.parse_args()
    if args.cmd == 'serve':
        serve(args)
    else:
        asyncio.run(play(args))


if __name__ == '__main__':
    main()
