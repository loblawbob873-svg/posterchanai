"""Articles (NIP-23 long-form) between two real accounts on this node's REAL relay.

Every article, draft, edit and comment is made in the shipped client's own editor (Write article -> title,
summary, body -> Publish / Save draft; the reader's ✎ Edit; "Write a comment"), on one account's device, and read
on another account's FRESH device (nothing cached) through the shipped RelayServer
(tests/client/discover_flows_harness.py).

  write/read/edit  Ann publishes; Bob's Articles lists it and his reader shows it; Bob comments; Ann edits the
                   title from a second device and Bob's list shows ONE card at the new title (an edit, not a copy);
                   Ann's list counts Bob's comment.
  drafts           a draft saved on one device is listed on a fresh one, Resume brings its title and body back,
                   and publishing it from there clears the draft on every device.
  no answer        the relay answering late still lists the articles; a relay that never answers a fresh
                   device's read says so with a retry -- never "No articles yet", which tells a reader there is
                   nothing to read -- and the retry lists them once it answers.

Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/discover.js=/tmp/old/discover.js".
"""
import json

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('articles',)
FIRST_FILL = 90
SCENARIOS = {}
COST = {'an_article_is_read_commented_and_its_edit_shows_on_another_account': 40,
        'a_draft_resumes_on_a_fresh_device_and_publishing_clears_it': 30}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


class Ctx:
    def __init__(self, n):
        self.gate = F.Gate(n.home)


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


SET = """((sel,v)=>{const x=document.querySelector(sel);x.value=v;x.dispatchEvent(new Event('input',{bubbles:true}));return true})"""
CARDS = "[...document.querySelectorAll('#art-list .article-card')]"
TITLES = CARDS + ".map(c=>c.querySelector('.art-title').textContent)"


async def open_articles(d):
    await d.b.js("__PC.switchView('articles');true")
    await F.wait(d, "!!document.querySelector('#art-new') && !document.querySelector('#art-list .spinner')", 40,
                 'Articles loaded')


async def write(d, title, body, summary=''):
    """Articles -> Write article -> type -> (the editor stays open)."""
    await open_articles(d)
    await d.b.js("document.querySelector('#art-new').click();true")
    await F.wait(d, "!!document.querySelector('#ae-title') && !!document.querySelector('#ae-pub')", 20, 'the editor')
    await d.b.js(SET + "('#ae-title',%s)" % json.dumps(title))
    await d.b.js(SET + "('#ae-sum',%s)" % json.dumps(summary))
    await d.b.js(SET + "('#ae-body',%s)" % json.dumps(body))


def articles(n, pk, kind=30023):
    return n.home.query({'kinds': [kind], 'authors': [pk]})


def d_of(ev):
    return next(t[1] for t in ev['tags'] if t[0] == 'd')


def title_of(ev):
    return next((t[1] for t in ev['tags'] if t[0] == 'title'), '')


async def open_card(d, title):
    await F.wait(d, "%s.some(c=>c.querySelector('.art-title').textContent===%s)" % (CARDS, json.dumps(title)), 40,
                 'the article %r listed' % title)
    await d.b.js("%s.find(c=>c.querySelector('.art-title').textContent===%s).click();true" % (CARDS, json.dumps(title)))
    await F.wait(d, "!!document.querySelector('.article-view .av-title') && "
                    "document.querySelector('.article-view .av-title').textContent===%s" % json.dumps(title), 30,
                 'the reader open on %r' % title)


@scenario
async def an_article_is_read_commented_and_its_edit_shows_on_another_account(S):
    n, ann_pk = S.n, S.pk(0)
    ann = await S.device(0)
    await write(ann, 'Field notes %d' % S.base, '## Day one\n\nThe **heron** came back at dawn.', 'A week by the marsh')
    await ann.b.js("document.querySelector('#ae-pub').click();true")
    got = await F.until(lambda: articles(n, ann_pk), 30, 'the article on the relay', ann)
    slug = d_of(got[0])

    bob = await S.device(1)                                  # another account, a fresh device
    await open_articles(bob)
    await open_card(bob, 'Field notes %d' % S.base)
    shown = await bob.b.js("document.querySelector('.article-view .av-body').innerText")
    assert 'heron came back at dawn' in shown and 'Day one' in shown, shown
    assert await bob.b.js("!!document.querySelector('.article-view .av-body strong, .article-view .av-body b')"), \
        'the markdown was not rendered'
    # Bob comments from the reader.
    await F.wait(bob, "!document.querySelector('#av-comment-list .spinner')", 30, 'the comments loaded')
    await bob.b.js("document.querySelector('#av-comment').click();true")
    await F.wait(bob, "!!document.querySelector('#cmp') && !!document.querySelector('#cmp-send')", 20, 'the comment composer')
    await bob.b.js("(()=>{const t=document.querySelector('#cmp');t.value='Lovely notes, Ann';"
                   "t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-send').click();return true})()")
    addr = '30023:%s:%s' % (ann_pk, slug)
    await F.until(lambda: n.home.query({'kinds': [1, 1111], '#A': [addr]}) or n.home.query({'kinds': [1, 1111], '#a': [addr]}),
                  30, 'the comment on the relay', bob)

    # Ann edits the title from a SECOND device (fresh): the reader's ✎ -> new title -> Publish.
    ann2 = await S.device(0)
    await open_articles(ann2)
    await open_card(ann2, 'Field notes %d' % S.base)
    await F.wait(ann2, "!!document.querySelector('#av-edit')", 20, 'the edit button on her own article')
    await ann2.b.js("document.querySelector('#av-edit').click();true")
    await F.wait(ann2, "!!document.querySelector('#ae-title') && document.querySelector('#ae-title').value===%s"
                 % json.dumps('Field notes %d' % S.base), 20, 'the editor holding the article')
    assert 'heron' in await ann2.b.js("document.querySelector('#ae-body').value")
    await ann2.b.js(SET + "('#ae-title',%s)" % json.dumps('Marsh diary %d' % S.base))
    await ann2.b.js("document.querySelector('#ae-pub').click();true")
    await F.until(lambda: [e for e in articles(n, ann_pk) if title_of(e) == 'Marsh diary %d' % S.base], 30,
                  'the edit on the relay', ann2)
    held = articles(n, ann_pk)
    assert len(held) == 1 and d_of(held[0]) == slug, ('the edit published a second article', [title_of(e) for e in held])

    await open_articles(bob)
    await F.wait(bob, "%s.includes(%s)" % (TITLES, json.dumps('Marsh diary %d' % S.base)), 30, 'the edit on Bob\'s list')
    titles = await bob.b.js(TITLES)
    assert 'Field notes %d' % S.base not in titles, ('the old version is still listed', titles)
    # Ann's list counts Bob's comment on the card.
    await open_articles(ann2)
    await F.wait(ann2, "%s.some(c=>c.querySelector('.art-title').textContent===%s && /💬 1/.test(c.innerText))"
                 % (CARDS, json.dumps('Marsh diary %d' % S.base)), 30, 'the comment counted on Ann\'s card')


@CHROME
def test_an_article_is_read_commented_and_its_edit_shows_on_another_account(ran):
    ran('an_article_is_read_commented_and_its_edit_shows_on_another_account')


DRAFTS = "[...document.querySelectorAll('#art-drafts .draft-art .da-title')].map(x=>x.textContent)"


@scenario
async def a_draft_resumes_on_a_fresh_device_and_publishing_clears_it(S):
    n, pk = S.n, S.pk(0)
    title = 'Half-written %d' % S.base
    one = await S.device(0)
    await write(one, title, 'First paragraph, more to come.')
    await one.b.js("document.querySelector('#ae-draft').click();window.__savedAt=performance.now();true")
    await F.until(lambda: [e for e in articles(n, pk, 30024) if title_of(e) == title], 30, 'the draft on the relay', one)
    assert not articles(n, pk), 'saving a draft published the article'

    two = await S.device(0)                                  # the same account, a fresh device
    await open_articles(two)
    await F.wait(two, "%s.some(t=>t.includes(%s))" % (DRAFTS, json.dumps(title)), 30, 'the draft listed')
    await two.b.js("[...document.querySelectorAll('#art-drafts .draft-art')].find(c=>c.innerText.includes(%s))"
                   ".querySelector('.da-edit').click();true" % json.dumps(title))
    await F.wait(two, "!!document.querySelector('#ae-title') && document.querySelector('#ae-title').value===%s"
                 % json.dumps(title), 20, 'Resume opened the draft')
    assert await two.b.js("document.querySelector('#ae-body').value") == 'First paragraph, more to come.'
    await two.b.js(SET + "('#ae-body',%s)" % json.dumps('First paragraph, more to come.\n\nAnd now it is done.'))
    # Published within a second of the last keystroke -- the way a person finishes a sentence and presses Publish.
    await two.b.js("document.querySelector('#ae-pub').click();window.__pubAt=performance.now();true")
    await F.until(lambda: [e for e in articles(n, pk) if title_of(e) == title], 30, 'the article published', two)
    await F.wait(two, "__PC.isView('articles') && !document.querySelector('#art-list .spinner')", 30, 'back on Articles')
    # The draft is gone everywhere: from the relay, and from a third fresh device's list.
    await F.until(lambda: not [e for e in articles(n, pk, 30024) if title_of(e) == title], 30,
                  'the published draft deleted from the relay', two)
    three = await S.device(0)
    await open_articles(three)
    await F.wait(three, "%s.includes(%s)" % (TITLES, json.dumps(title)), 30, 'the article listed')
    assert not await three.b.js("%s.some(t=>t.includes(%s))" % (DRAFTS, json.dumps(title))), (
        'the draft outlived its publish', [(e['created_at'], title_of(e)) for e in articles(n, pk, 30024)],
        n.home.query({'kinds': [5], 'authors': [pk]}))
    body = articles(n, pk)[0]['content']
    assert 'And now it is done.' in body, body
    # The editors' autosave windows (4 s after the last keystroke) have passed on BOTH devices: nothing came back
    # as a draft, and the publishing device showed no error over its own success.
    for d, mark in ((one, '__savedAt'), (two, '__pubAt')):
        await F.wait(d, "performance.now()-window.%s>5000" % mark, 15, 'the autosave window passed')
    assert not [e for e in articles(n, pk, 30024) if title_of(e) == title], (
        'the draft was saved again after it was published', [(e['created_at'], title_of(e)) for e in articles(n, pk, 30024)])
    shown = await two.b.js("window.__toastLog||[]")
    assert not [t for t in shown if 'failed' in t], ('an error toast after a successful publish', shown)


@CHROME
def test_a_draft_resumes_on_a_fresh_device_and_publishing_clears_it(ran):
    ran('a_draft_resumes_on_a_fresh_device_and_publishing_clears_it')


@scenario
async def a_relay_that_never_answers_is_not_no_articles_yet(S):
    n, gate = S.n, S.ctx.gate
    ann = await S.device(0)
    title = 'Lighthouse log %d' % S.base
    await write(ann, title, 'The lamp turned all night.')
    await ann.b.js("document.querySelector('#ae-pub').click();true")
    await F.until(lambda: articles(n, S.pk(0)), 30, 'the article on the relay', ann)

    tag = 'art%d' % S.base
    mine = lambda c: F.device_of(c) == tag   # noqa: E731
    # Late: the relay answers this device's article reads 3 s late -- the list still arrives.
    late = gate.add(lambda fl, c: 3 if mine(c) and F.asks(fl, kinds=(30023, 30024)) else None)
    bob = await S.device(1, tag=tag)
    await open_articles(bob)
    await F.wait(bob, "%s.includes(%s)" % (TITLES, json.dumps(title)), 30, 'the late answer listed it')
    gate.drop(late)

    # Never: nobody answers. The screen must say so, never claim there is nothing to read.
    never = gate.add(lambda fl, c: 'never' if mine(c) and F.asks(fl, kinds=(30023, 30024)) else None)
    try:
        await bob.b.js("__PC.switchView('home');true")
        await bob.b.js("__PC.switchView('articles');true")
        await F.wait(bob, "!!document.querySelector('#art-list') && !document.querySelector('#art-list .spinner')", 40,
                     'Articles gave up waiting')
        text = await bob.b.js("document.querySelector('#art-list').innerText")
        assert 'No articles yet' not in text, ('an unanswered read was reported as no articles', text)
        assert await bob.b.js("!!document.querySelector('#art-retry')"), text
        gate.drop(never)
        await bob.b.js("document.querySelector('#art-retry').click();true")
        await F.wait(bob, "%s.includes(%s)" % (TITLES, json.dumps(title)), 30, 'the retry listed it')
    finally:
        gate.drop(never)


@CHROME
def test_a_relay_that_never_answers_is_not_no_articles_yet(ran):
    ran('a_relay_that_never_answers_is_not_no_articles_yet')
