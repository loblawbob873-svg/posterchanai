"""News, Server Stats and My Analytics, driven end to end on a REAL node with the node's REAL routers.

  analytics  one account posts with the shipped composer; two others react with a card's react button and reply
             with its reply button; the author's FRESH device shows the counts (a reply here is the NIP-22 kind 1111
             the client publishes). A relay that never answers the author's read gets the error card and a retry
             -- never "Publish your first post", which tells somebody with posts that they have none.
  stats      Discover -> Server Stats over the real /client/server-stats route (stats_service asking THIS node's
             relay store for its aggregates, nostr_relay/aggregates.py) counts a post a member just published,
             and the Relay and Uptime tabs draw; a route answering 503 says so with a Try again that recovers --
             never a spinner for ever.
  news       a feed added with ＋ is read through the real /api/rss route (rss_service: fetch, parse, shared
             cache) from a real HTTP publisher serving RSS 2.0, and a FRESH device shows its stories; a fresh
             device whose relay never answers the feed-list read says it could not load the list and writes
             NOTHING (the old screen offered "No feeds yet — tap ＋" and published the one feed added there as
             the whole list); /api/rss answering 503 says it could not fetch, with a retry.

The AI summary on a News story is AI and out of scope. Harness: tests/client/discover_flows_harness.py.
Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/news.js=/tmp/old/news.js,...".
"""
import asyncio
import json
import tempfile
import threading
import time
from email.utils import formatdate
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('news', 'stats', 'analytics')
FIRST_FILL = 130
SCENARIOS = {}
COST = {'my_analytics_counts_what_other_accounts_did_to_my_posts': 35,
        'a_fresh_device_nobody_answers_never_replaces_the_feed_list': 30}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# ------------------------------------------------------------------------------------------------ the publisher
class Publisher:
    """A website publishing RSS 2.0 feeds: https://<name>.example.test/<feed>.xml is served by this HTTP server
    (the node's fetch is pointed here by host -- DNS is not under test). Each feed's stories are what a site would
    publish: title, link, pubDate, description."""

    def __init__(self):
        self.feeds = {}             # (host, path) -> [(title, minutes ago)]
        pub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                host, _, path = self.path.lstrip('/').partition('/')
                items = pub.feeds.get((host, '/' + path))
                if items is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = ''.join(
                    '<item><title>%s</title><link>https://%s/%s</link><guid>https://%s/%s</guid>'
                    '<pubDate>%s</pubDate><description>%s, reported.</description></item>'
                    % (t, host, t.lower().replace(' ', '-'), host, t.lower().replace(' ', '-'),
                       formatdate(time.time() - 60 * ago), t) for t, ago in items)
                data = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>%s</title>'
                        '<link>https://%s/</link><description>news</description>%s</channel></rss>'
                        % (host, host, body)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/rss+xml')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def feed(self, host, path, titles):
        self.feeds[(host, path)] = [(t, i * 7 + 3) for i, t in enumerate(titles)]
        return 'https://%s%s' % (host, path)

    def transport(self, pinned=False):   # rss_service asks for its direct leg pinned; the fake web is one host
        import httpx
        port = self.http.server_address[1]

        class ToPublisher(httpx.AsyncBaseTransport):
            def __init__(self):
                self.inner = httpx.AsyncHTTPTransport()

            async def handle_async_request(self, request):
                url = request.url
                request.url = httpx.URL('http://127.0.0.1:%d/%s%s' % (port, url.host, url.raw_path.decode()))
                return await self.inner.handle_async_request(request)

            async def aclose(self):
                await self.inner.aclose()
        return ToPublisher()

    def stop(self):
        self.http.shutdown()


# ------------------------------------------------------------------------------------------------ the node
class Ctx:
    def __init__(self, n):
        from fastapi import APIRouter
        from app.routers import client, rss
        from app.services import rss_service, settings_store, stats_service
        from app.services.nostr_relay import aggregates
        self.mp = mp = pytest.MonkeyPatch()
        self.n = n
        self.publisher = Publisher()
        # News: the node fetches feeds for real (rss_service: fetch, parse, shared cache) -- from the publisher.
        mp.setattr(rss_service, 'is_safe_host', lambda url: url.split('/')[2].endswith('.example.test'))
        mp.setattr(rss_service, 'afallback_transport', self.publisher.transport)
        # Stats: the relay answers its own aggregates from its own store (the relay process's ask poller, run on
        # this node's relay loop over a private control dir).
        self.ctrl = tempfile.mkdtemp(prefix='pc-stats-ctrl-')
        self.stop_ev = asyncio.Event()
        n.loop.loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(aggregates.ask_poller(n.home.store, self.ctrl, self.stop_ev, interval=0.1)))
        mp.setattr(aggregates, 'ask', partial(aggregates.ask, control_dir=self.ctrl, check_running=False))
        mp.setattr(stats_service, '_TTL', 0.0)
        mp.setattr(stats_service, '_cache', {'at': 0.0, 'data': None})
        mp.setattr(stats_service, '_lock', asyncio.Lock())
        mp.setattr(settings_store, '_port', lambda db=None: n.home.port)
        r = APIRouter()
        r.add_api_route('/client/server-stats', client.client_server_stats, methods=['GET'])
        r.add_api_route('/client/uptime', client.client_uptime, methods=['GET'])
        self.api = F.Routers(mp, [(r, ''), (rss.router, '')], F.members(FIRST_FILL, SCENARIOS),
                             paths=['/client/server-stats', '/client/uptime', '/api/rss/'])
        self.gate = F.Gate(n.home)

    def stop(self):
        self.api.stop()
        self.n.loop.loop.call_soon_threadsafe(self.stop_ev.set)
        self.publisher.stop()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


# ------------------------------------------------------------------------------------------------ analytics

METRICS = ("Object.fromEntries([...document.querySelectorAll('.ua-metric')].map(m=>[m.querySelector('span').textContent.trim(),"
           "m.querySelector('strong').textContent.replace(/\\s+/g,' ').trim()]))")


async def reply(d, note_id, text):
    """The post's reply button -> type -> Send."""
    card = F.CARD % note_id
    await F.wait(d, "!!%s && !!%s.querySelector('.act[data-a=reply]')" % (card, card), 30, 'the post card')
    await d.b.js("%s.querySelector('.act[data-a=reply]').click();true" % card)
    await F.wait(d, "!!document.querySelector('#cmp') && !!document.querySelector('#cmp-send')", 20, 'the reply composer')
    await d.b.js("(()=>{const t=document.querySelector('#cmp');t.focus();t.value=%s;"
                 "t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-send').click();return true})()"
                 % json.dumps(text))
    pk = await d.b.js('__PC.me().pubkey')
    await F.until(lambda: [e for e in d.node.home.query({'kinds': [1, 1111], 'authors': [pk], '#e': [note_id]})
                           if e['content'] == text], 30, 'the reply on the home relay', d)


async def open_analytics(d):
    await d.b.js("__PC.switchView('analytics');true")


@scenario
async def my_analytics_counts_what_other_accounts_did_to_my_posts(S):
    a = await S.device(0)
    one = await F.post_note(a, 'Morning light over the bay (%d)' % S.base)
    two = await F.post_note(a, 'Evening tide report (%d)' % S.base)
    b = await S.device(1)
    await F.open_note(b, one['id'])
    await F.react(b, one['id'], '🔥')
    await reply(b, one['id'], 'Gorgeous, where was this?')
    c = await S.device(2)
    await F.open_note(c, two['id'])
    await F.react(c, two['id'], '👍')

    a2 = await S.device(0)                                  # the author's fresh device
    await open_analytics(a2)
    await F.wait(a2, "document.querySelectorAll('.ua-metric').length>0", 40, 'the analytics drawn')
    m = await a2.b.js(METRICS)
    assert m['Posts'] == '2', m
    assert m['Reactions'] == '2', m
    assert m['Replies'] == '1', ('a reply made with the reply button was not counted', m)
    assert m['Total engagement'] == '3', m
    top = await a2.b.js("[...document.querySelectorAll('.ua-posts [data-note]')].map(x=>x.dataset.note)")
    assert top and top[0] == one['id'], ('the post with a reaction AND a reply is not the top post', top)


@CHROME
def test_my_analytics_counts_what_other_accounts_did_to_my_posts(ran):
    ran('my_analytics_counts_what_other_accounts_did_to_my_posts')


@scenario
async def analytics_nobody_answers_is_an_error_not_a_greeting(S):
    a = await S.device(0)
    await F.post_note(a, 'Only post (%d)' % S.base)
    tag = 'ua%d' % S.base
    pk = S.pk(0)
    rule = S.ctx.gate.add(lambda fl, c: 'never' if F.device_of(c) == tag and F.asks(fl, kinds=(1,), authors=(pk,)) else None)
    try:
        a2 = await S.device(0, tag=tag)
        await open_analytics(a2)
        await F.wait(a2, "!document.querySelector('#feed .spinner')", 40, 'analytics gave up waiting')
        text = await a2.b.js("document.querySelector('#feed').innerText")
        assert 'Publish your first post' not in text, ('an unanswered read greeted the author as having no posts', text)
        assert await a2.b.js("!!document.querySelector('#ua-retry')"), text
        S.ctx.gate.drop(rule)
        await a2.b.js("document.querySelector('#ua-retry').click();true")
        await F.wait(a2, "document.querySelectorAll('.ua-metric').length>0", 40, 'the retry drew the numbers')
        assert (await a2.b.js(METRICS))['Posts'] == '1'
    finally:
        S.ctx.gate.drop(rule)


@CHROME
def test_analytics_nobody_answers_is_an_error_not_a_greeting(ran):
    ran('analytics_nobody_answers_is_an_error_not_a_greeting')


# ------------------------------------------------------------------------------------------------ stats

TILES = ("Object.fromEntries([...document.querySelectorAll('.st-tile')].map(t=>[t.querySelector('.st-tlbl').textContent.trim(),"
         "t.querySelector('.st-tval').textContent.trim()]))")


async def open_stats(d, tab='stats'):
    await d.b.js("__PC.switchView('stats');true")
    await F.wait(d, "!!document.querySelector('.st-tab[data-tab=\"%s\"]')" % tab, 30, 'Server Stats')
    await d.b.js("document.querySelector('.st-tab[data-tab=\"%s\"]').click();true" % tab)


@scenario
async def server_stats_count_a_post_published_here(S):
    a = await S.device(0)
    await F.post_note(a, 'Counting me in the stats (%d)' % S.base)
    b = await S.device(1)
    await open_stats(b)
    await F.wait(b, "!!document.querySelector('.st-tile')", 40, 'the Activity tab drawn')
    tiles = await b.b.js(TILES)
    assert tiles.get('notes') not in (None, '—', '0'), ('the post published here is not counted', tiles)
    assert int(tiles['notes'].replace(',', '')) >= 1
    assert 'could not be asked' not in await b.b.js("document.querySelector('#feed').innerText")
    await b.b.js("document.querySelector('.st-tab[data-tab=\"relay\"]').click();true")
    await F.wait(b, "!document.querySelector('.st-body .spinner') && document.querySelector('.st-body').innerText.length>40",
                 30, 'the Relay tab drawn')
    await b.b.js("document.querySelector('.st-tab[data-tab=\"uptime\"]').click();true")
    await F.wait(b, "!document.querySelector('.st-body .spinner') && document.querySelector('.st-body').innerText.length>10",
                 30, 'the Uptime tab drawn')


@CHROME
def test_server_stats_count_a_post_published_here(ran):
    ran('server_stats_count_a_post_published_here')


@scenario
async def stats_the_server_cannot_answer_say_so_and_retry_recovers(S):
    api, tag = S.ctx.api, 'st%d' % S.base                  # the faults hit THIS device only
    api.fail('/client/server-stats', 503, tag)
    api.fail('/client/uptime', 'drop', tag)
    try:
        b = await S.device(0, tag=tag)
        await open_stats(b)
        await F.wait(b, "!!document.querySelector('.st-body .st-retry')", 30, 'the Activity tab said it could not load')
        assert not await b.b.js("!!document.querySelector('.st-body .spinner')")
        await b.b.js("document.querySelector('.st-tab[data-tab=\"uptime\"]').click();true")
        await F.wait(b, "!!document.querySelector('.st-body .st-retry')", 30, 'the Uptime tab said it could not load')
        # Back on Activity: the error is still on screen while a fresh ask is out, and the node is slow to refuse
        # it. The person presses Try again right then -- just as the node recovers.
        api.fail('/client/server-stats', (503, 2), tag)
        await b.b.js("document.querySelector('.st-tab[data-tab=\"stats\"]').click();true")
        await F.wait(b, "!!document.querySelector('.st-body .st-retry')", 30, 'still saying so')
        api.heal('/client/server-stats', tag)
        await b.b.js("document.querySelector('.st-body .st-retry').click();true")
        await F.wait(b, "!!document.querySelector('.st-tile')", 30, 'Try again drew the stats')
    finally:
        api.heal('/client/server-stats', tag)
        api.heal('/client/uptime', tag)


@CHROME
def test_stats_the_server_cannot_answer_say_so_and_retry_recovers(ran):
    ran('stats_the_server_cannot_answer_say_so_and_retry_recovers')


# ------------------------------------------------------------------------------------------------ news

STORIES = "[...document.querySelectorAll('#news-list .news-title')].map(a=>a.textContent)"


async def open_news(d):
    await d.b.js("__PC.switchView('news');true")
    await F.wait(d, "!!document.querySelector('#news-list') && !document.querySelector('#news-list .spinner')", 40,
                 'News loaded')


async def add_feed(d, url, name):
    """News -> ＋ -> the URL and a name -> Add feed."""
    await F.wait(d, "!!document.querySelector('.news-add')", 20, 'the ＋ button')
    await d.b.js("document.querySelector('.news-add').click();true")
    await F.wait(d, "!!document.querySelector('#news-newurl')", 15, 'Manage feeds')
    await d.b.js("(()=>{document.querySelector('#news-newurl').value=%s;document.querySelector('#news-newname').value=%s;"
                 "document.querySelector('#news-addbtn').click();return true})()" % (json.dumps(url), json.dumps(name)))


def feed_list(n, pk):
    docs = F.stored(n, {'kinds': [30078], 'authors': [pk], '#d': ['pcai:news-feeds']})
    if not docs:
        return None
    return [f['url'] for f in json.loads(max(docs, key=lambda e: e['created_at'])['content'])]


@scenario
async def a_feed_added_on_one_device_reads_on_a_fresh_one(S):
    host = 'harbour%d.example.test' % S.base
    url = S.ctx.publisher.feed(host, '/rss.xml', ['Ferry timetable changes', 'New lighthouse keeper named'])
    a = await S.device(0)
    await open_news(a)
    await add_feed(a, url, 'Harbour Gazette')
    await F.wait(a, "%s.includes('Ferry timetable changes')" % STORIES, 40, 'the feed\'s stories')
    await F.until(lambda: feed_list(S.n, S.pk(0)) == [url], 30, 'the feed list on the relay', a)

    a2 = await S.device(0)                                  # the same account, a fresh device
    await open_news(a2)
    await F.wait(a2, "%s.includes('New lighthouse keeper named')" % STORIES, 40, 'the stories on the fresh device')
    meta = await a2.b.js("document.querySelector('#news-list .news-meta').textContent")
    assert 'Harbour Gazette' in meta, meta


@CHROME
def test_a_feed_added_on_one_device_reads_on_a_fresh_one(ran):
    ran('a_feed_added_on_one_device_reads_on_a_fresh_one')


@scenario
async def a_fresh_device_nobody_answers_never_replaces_the_feed_list(S):
    n, pk = S.n, S.pk(0)
    pub = S.ctx.publisher
    one = pub.feed('alpha%d.example.test' % S.base, '/feed.xml', ['Alpha story'])
    two = pub.feed('beta%d.example.test' % S.base, '/feed.xml', ['Beta story'])
    three = pub.feed('gamma%d.example.test' % S.base, '/feed.xml', ['Gamma story'])
    a = await S.device(0)
    await open_news(a)
    await add_feed(a, one, 'Alpha')
    await F.until(lambda: feed_list(n, pk) == [one], 30, 'the first feed saved', a)
    await open_news(a)
    await add_feed(a, two, 'Beta')
    await F.until(lambda: feed_list(n, pk) == [one, two], 30, 'both feeds saved', a)

    tag = 'news%d' % S.base
    rule = S.ctx.gate.add(lambda fl, c: 'never' if F.device_of(c) == tag and F.asks(fl, kinds=(30078,), authors=(pk,)) else None)
    try:
        a2 = await S.device(0, tag=tag)
        await open_news(a2)
        # Whatever the screen offers, a person who sees no feeds adds one.
        if await a2.b.js("!!document.querySelector('.news-add')"):
            await add_feed(a2, three, 'Gamma')
        loop = asyncio.get_running_loop()
        end = loop.time() + 3
        while loop.time() < end:                            # the window a wrong publish lands in
            assert feed_list(n, pk) == [one, two], ('a device that never read the feed list replaced it', feed_list(n, pk))
            await asyncio.sleep(.2)
        text = await a2.b.js("document.querySelector('#news-list').innerText")
        assert 'No feeds yet' not in text, ('an unanswered read was shown as no feeds', text)
        assert await a2.b.js("!!document.querySelector('#news-retry')"), text
        S.ctx.gate.drop(rule)
        await a2.b.js("document.querySelector('#news-retry').click();true")
        await F.wait(a2, "%s.includes('Alpha story') && %s.includes('Beta story')" % (STORIES, STORIES), 40,
                     'Try again read both feeds')
        assert feed_list(n, pk) == [one, two]
    finally:
        S.ctx.gate.drop(rule)


@CHROME
def test_a_fresh_device_nobody_answers_never_replaces_the_feed_list(ran):
    ran('a_fresh_device_nobody_answers_never_replaces_the_feed_list')


@scenario
async def the_news_server_503_says_it_could_not_fetch(S):
    url = S.ctx.publisher.feed('valley%d.example.test' % S.base, '/news.xml', ['Bridge reopens'])
    a = await S.device(0)
    await open_news(a)
    await add_feed(a, url, 'Valley News')
    await F.wait(a, "%s.includes('Bridge reopens')" % STORIES, 40, 'the story')
    api, tag = S.ctx.api, 'rss%d' % S.base
    api.fail('/api/rss/', 503, tag)
    try:
        a2 = await S.device(0, tag=tag)                     # fresh: nothing on screen to fall back on
        await open_news(a2)
        await F.wait(a2, "!!document.querySelector('#news-retry')", 30, 'News said it could not fetch')
        text = await a2.b.js("document.querySelector('#news-list').innerText")
        assert 'No articles' not in text and 'No feeds' not in text, text
        api.heal('/api/rss/', tag)
        await a2.b.js("document.querySelector('#news-retry').click();true")
        await F.wait(a2, "%s.includes('Bridge reopens')" % STORIES, 40, 'Try again fetched it')
    finally:
        api.heal('/api/rss/', tag)


@CHROME
def test_the_news_server_503_says_it_could_not_fetch(ran):
    ran('the_news_server_503_says_it_could_not_fetch')
