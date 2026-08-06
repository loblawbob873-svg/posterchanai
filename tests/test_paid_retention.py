"""Pay-to-stay — the optional paid retention tier (app/services/paid_retention_service.py).

Two halves, and they pin the two different ways this feature can lose somebody something.

THE PRUNE half. Direct-published events — everything a client entrusted to this relay — were
untouchable by every age rule before this feature existed, so the tiered rules are the first thing
in the codebase that can delete them. What is pinned here is not just "it deletes old free-tier
notes" but the four ways it must REFUSE to: feature off, no free window, ledger unreadable, and
author with an account here. Three of those are the same failure — an empty subscriber set read as
"nobody paid" — which would delete exactly the notes somebody paid to keep.

THE MONEY half. A kind-9735 zap receipt is an ordinary event that anyone in the web of trust can
publish, claiming any amount from anyone. The ONLY thing that makes one a payment is its signature
by the zapper service of our own lightning address (NIP-57's `nostrPubkey`), so these tests forge
receipts — right shape, wrong signer; right signer, unsigned request; right everything but a zap of
a post rather than the profile — and demand each is refused. They use real BIP-340 signatures, not
stubs: a verifier that silently accepts a bad signature would pass any test written against a mock.
"""
import asyncio
import json
import time
import uuid

import pytest

from app.services import paid_retention_service as prs
from app.services.nostr import bip340
from app.services.nostr.event import build_event

DAY = 86400


# ---- bolt11 ------------------------------------------------------------

def test_bolt11_amounts_decode_to_millisats():
    """The invoice amount is authoritative (the zap request's `amount` tag is only a claim), so a
    misread multiplier is a mispriced subscription. Values from BOLT-11's own examples."""
    assert prs.decode_bolt11_msats("lnbc2500u1pvjluezpp5abcdef") == 250_000_000
    assert prs.decode_bolt11_msats("lnbc20m1pvjluezpp5qqqsyq") == 2_000_000_000
    assert prs.decode_bolt11_msats("lnbc1500n1pwyvqwfpp5x") == 150_000
    assert prs.decode_bolt11_msats("lntb10u1pvjluezhp58yjm") == 1_000_000   # testnet prefix
    assert prs.decode_bolt11_msats("lnbcrt100u1p3xyz") == 10_000_000        # regtest prefix
    # No amount (a "any amount" invoice), and garbage, must be 0 — never a default price.
    assert prs.decode_bolt11_msats("lnbc1pvjluezpp5qqqsyq") == 0
    assert prs.decode_bolt11_msats("") == 0
    assert prs.decode_bolt11_msats("not-an-invoice") == 0


# ---- receipt verification ----------------------------------------------

def _key():
    sk = uuid.uuid4().bytes + uuid.uuid4().bytes           # 32 bytes
    return sk, bip340.pubkey_from_seckey(sk).hex()


def _receipt(zapper_sk, payer_sk, recv_pub, *, msats=1_000_000, e_tag=None,
             request_override=None, amount_tag=True, invoice=None, claim=None):
    """A well-formed NIP-57 receipt: a kind 9735 signed by the zapper service, carrying the payer's
    signed kind-9734 request in its `description` tag.

    `msats` is what the INVOICE says (the `n` multiplier is 100 msats per unit, so it encodes the
    figure exactly); `claim` overrides only what the request's `amount` tag asks for, which is how
    the two are told apart."""
    tags = [["p", recv_pub], ["relays", "wss://x"]]
    if amount_tag:
        tags.append(["amount", str(msats if claim is None else claim)])
    if e_tag:
        tags.append(["e", e_tag])
    req = request_override or build_event(payer_sk, 9734, "", tags=tags)
    inv = invoice if invoice is not None else f"lnbc{msats // 100}n1pvjluezpp5{'q' * 20}"
    return build_event(zapper_sk, 9735, "", tags=[
        ["p", recv_pub], ["bolt11", inv], ["description", json.dumps(req)]]), req


def test_a_real_receipt_is_credited_to_the_payer():
    zap_sk, zap_pub = _key()
    payer_sk, payer_pub = _key()
    _, recv_pub = _key()
    # The invoice says 21k sats; the request CLAIMS ten times that. The invoice is what was paid.
    ev, _ = _receipt(zap_sk, payer_sk, recv_pub, msats=21_000_000, claim=210_000_000)
    got = prs.verify_receipt(ev, zap_pub, recv_pub)
    assert got is not None, "a correctly signed profile zap must be credited"
    payer, msats = got
    assert payer == payer_pub
    assert msats == 21_000_000, "the amount must come from the invoice, not the request's claim"


def test_a_receipt_signed_by_anyone_but_our_zapper_service_is_worthless():
    """THE check. Anyone in the WoT can publish a kind 9735 saying they paid a million sats; the
    signature of the LNURL endpoint's `nostrPubkey` is the only thing that means an invoice was
    actually settled."""
    impostor_sk, _ = _key()
    _, zap_pub = _key()                                    # the real zapper service, which did NOT sign
    payer_sk, _ = _key()
    _, recv_pub = _key()
    ev, _ = _receipt(impostor_sk, payer_sk, recv_pub)
    assert prs.verify_receipt(ev, zap_pub, recv_pub) is None


def test_a_tampered_receipt_is_refused():
    """Right signer, but the event was edited after signing (e.g. the amount inflated)."""
    zap_sk, zap_pub = _key()
    payer_sk, _ = _key()
    _, recv_pub = _key()
    ev, _ = _receipt(zap_sk, payer_sk, recv_pub)
    ev["tags"] = [t for t in ev["tags"] if t[0] != "bolt11"] + [["bolt11", "lnbc9999m1pxx"]]
    assert prs.verify_receipt(ev, zap_pub, recv_pub) is None


def test_a_forged_zap_request_is_refused():
    """The receipt is genuine but its embedded request is not signed by the pubkey it names — which
    is how a payer would be impersonated to credit the wrong account (or an unpaid one)."""
    zap_sk, zap_pub = _key()
    payer_sk, _ = _key()
    _, victim_pub = _key()
    _, recv_pub = _key()
    real = build_event(payer_sk, 9734, "", tags=[["p", recv_pub]])
    real["pubkey"] = victim_pub                             # claim someone else made the payment
    ev, _ = _receipt(zap_sk, payer_sk, recv_pub, request_override=real)
    assert prs.verify_receipt(ev, zap_pub, recv_pub) is None


def test_a_zap_addressed_to_someone_else_is_refused():
    zap_sk, zap_pub = _key()
    payer_sk, _ = _key()
    _, recv_pub = _key()
    _, other_pub = _key()
    ev, _ = _receipt(zap_sk, payer_sk, other_pub)
    assert prs.verify_receipt(ev, zap_pub, recv_pub) is None


def test_a_zap_of_a_post_stays_a_tip():
    """Only a PROFILE zap buys storage. Otherwise every tip on any of the operator's posts silently
    becomes a storage purchase, and the operator can no longer be tipped for a post at all."""
    zap_sk, zap_pub = _key()
    payer_sk, _ = _key()
    _, recv_pub = _key()
    ev, _ = _receipt(zap_sk, payer_sk, recv_pub, e_tag="b" * 64)
    assert prs.verify_receipt(ev, zap_pub, recv_pub) is None


def test_no_amount_anywhere_credits_nothing():
    zap_sk, zap_pub = _key()
    payer_sk, _ = _key()
    _, recv_pub = _key()
    ev, _ = _receipt(zap_sk, payer_sk, recv_pub, amount_tag=False)
    ev["tags"] = [t for t in ev["tags"] if t[0] != "bolt11"]
    assert prs.verify_receipt(ev, zap_pub, recv_pub) is None


def test_an_unreadable_invoice_is_not_replaced_by_the_payers_own_claim():
    """The `amount` tag is the one number in a receipt the payer controls. If there IS an invoice
    and we can't read it, refusing costs one payment; trusting the claim sells storage for free."""
    zap_sk, zap_pub = _key()
    payer_sk, _ = _key()
    _, recv_pub = _key()
    # Signed by the real zapper service WITH the unreadable invoice — so this fails on the amount
    # rule, not on the signature (which would make the test pass for the wrong reason).
    ev, req = _receipt(zap_sk, payer_sk, recv_pub, msats=1_000_000, invoice="not-an-invoice")
    assert ("amount", "1000000") in [(t[0], t[1]) for t in req["tags"]], "the claim is there to fall back to"
    from app.services.nostr.event import verify_event
    assert verify_event(ev), "the receipt itself must be validly signed for this test to mean anything"
    assert prs.verify_receipt(ev, zap_pub, recv_pub) is None


# ---- crediting ---------------------------------------------------------

def test_renewing_early_adds_to_the_time_already_paid_for():
    ledger = prs._empty()
    now = int(time.time())
    prs._credit(ledger, "c" * 64, 30 * DAY, msats=1000)
    first = ledger["subs"]["c" * 64]["until"]
    prs._credit(ledger, "c" * 64, 30 * DAY, msats=1000)
    second = ledger["subs"]["c" * 64]["until"]
    assert first >= now + 29 * DAY
    assert second - first >= 29 * DAY, "a renewal must extend the expiry, not restart it from today"


def test_one_zap_cannot_buy_a_century():
    ledger = prs._empty()
    prs._credit(ledger, "d" * 64, 500 * 365 * DAY, msats=10 ** 12)
    horizon = int(time.time()) + prs._MAX_HORIZON_DAYS * DAY
    assert ledger["subs"]["d" * 64]["until"] <= horizon


def test_the_ledger_shape_survives_a_corrupt_document():
    """`_normalize` is what stands between a mangled/foreign relay document and the subscriber set.
    It must drop junk rather than raise — but it must also not turn a non-document into an empty
    ledger silently, which is why a non-object raises."""
    ok = prs._normalize({"updated": 1, "cursor": 2, "subs": {
        "e" * 64: {"until": 99, "msats": 5},
        "short": {"until": 99},                              # not a pubkey
        "f" * 64: "nonsense",                                # not a record
    }, "credited": {"id1": 7}})
    assert set(ok["subs"]) == {"e" * 64}
    assert ok["subs"]["e" * 64]["until"] == 99
    assert ok["credited"] == {"id1": 7}
    with pytest.raises(ValueError):
        prs._normalize(["not", "a", "document"])


def test_policy_reports_nothing_while_the_feature_is_off(monkeypatch):
    """Every node ships with this off; the policy the relay advertises and the landing page renders
    must be inert, not merely zeroed with a price still showing."""
    from app.services import settings_store
    monkeypatch.setattr(settings_store, "get_bool", lambda k, d=False: False)
    monkeypatch.setattr(settings_store, "get_int", lambda k, d=0: 999)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: "someone@example.com")
    pol = prs.policy()
    assert pol == {"enabled": False, "free_days": 0, "paid_days": 0, "sats_per_month": 0,
                   "lud16": "", "pubkey": ""}


# ---- the prune (needs Postgres) ----------------------------------------

psycopg2 = pytest.importorskip("psycopg2")

from app.services.nostr_relay.store import RelayStore            # noqa: E402

DSN = "host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan"


def _admin():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:
        pytest.skip(f"Postgres not reachable for the relay store: {e}")
    conn.autocommit = True
    return conn


@pytest.fixture
def store_factory():
    """Opened RelayStores in a scratch schema (the `posterchan` role can't CREATE DATABASE), so an
    unqualified table can only resolve inside it and a mistake errors instead of touching the live
    relay. Mirrors tests/test_relay_prune.py."""
    schema = "pcai_paid_test_" + uuid.uuid4().hex[:10]
    conn = _admin()
    conn.cursor().execute(f'CREATE SCHEMA "{schema}"')
    conn.close()
    dsn = DSN + f" options=-csearch_path={schema}"
    made = []

    def _make(loop, **kw):
        st = RelayStore(dsn, **kw)
        st.open(loop)
        made.append(st)
        return st

    try:
        yield _make
    finally:
        for st in made:
            st.close()
        try:
            c = _admin()
            c.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            c.close()
        except Exception:
            pass


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn(loop))
    finally:
        loop.close()


def _ev(i, *, pubkey, age_days=0, kind=1):
    return {"id": f"{i:064x}", "pubkey": pubkey, "kind": kind,
            "created_at": int(time.time()) - age_days * DAY, "content": f"note {i}",
            "tags": [], "sig": "0" * 128}


STRANGER = "1" * 64          # a WoT author with no account here
SUBSCRIBER = "2" * 64        # …who paid
LOCAL = "3" * 64             # a user of this instance (preserve set)


async def _seed(store):
    """30 old + 30 fresh direct-published notes for each of the three authors."""
    evs = []
    n = 0
    for pk in (STRANGER, SUBSCRIBER, LOCAL):
        for age in (90, 1):
            for _ in range(30):
                n += 1
                evs.append(_ev(n, pubkey=pk, age_days=age))
    await store.add_events_bulk(evs, origin="direct")


def test_off_by_default_no_direct_write_is_ever_touched(store_factory):
    """The state every node deploys in. A 30-day feed retention is set, the notes are 90 days old,
    and not one of them may go: they were published HERE."""
    async def go(loop):
        store = store_factory(loop, retention_days=30)
        await _seed(store)
        before = await store.count()
        preview = await store.prune_preview()
        removed = await store.prune()
        assert preview["total"] == removed == 0, "pay-to-stay off must delete nothing direct"
        assert await store.count() == before
        assert "aged_free" not in preview
    _run(go)


def test_free_tier_ages_out_only_unpaid_strangers(store_factory):
    """The feature working: the stranger's old notes go, the subscriber's and the local user's stay,
    and nothing recent goes for anyone."""
    async def go(loop):
        store = store_factory(loop, retention_days=30)
        store.set_preserve_pubkeys([LOCAL])
        store.free_retention_days = 30
        store.set_subscribers([SUBSCRIBER], ledger_ok=True)
        await _seed(store)

        preview = await store.prune_preview()
        removed = await store.prune()
        assert preview["aged_free"] == 30
        assert preview["total"] == removed == 30, "only the stranger's 30 old notes"

        left = {pk: len(await store.query([{"authors": [pk], "limit": 500}]))
                for pk in (STRANGER, SUBSCRIBER, LOCAL)}
        assert left == {STRANGER: 30, SUBSCRIBER: 60, LOCAL: 60}
    _run(go)


def test_a_subscribers_own_window_still_applies(store_factory):
    """A paid tier with a finite window is a LONGER window, not an exemption — otherwise 'paid days'
    silently means 'forever' and the setting does nothing."""
    async def go(loop):
        store = store_factory(loop, retention_days=0)
        store.free_retention_days = 30
        store.paid_retention_days = 60
        store.set_subscribers([SUBSCRIBER], ledger_ok=True)
        await _seed(store)
        preview = await store.prune_preview()
        removed = await store.prune()
        # 90 days old is past both windows; the local user is not in preserve here, so they're
        # treated as an unpaid stranger too — 3 authors, 30 old notes each, minus nothing.
        assert preview["aged_free"] == 60 and preview["aged_paid"] == 30
        assert removed == 90
        assert await store.count() == 90
    _run(go)


def test_an_unreadable_ledger_deletes_nothing(store_factory):
    """THE data-loss case. The relay could not read who has paid. Treating that as 'nobody has'
    deletes precisely the notes people paid to keep, so the tiered rules must not run at all —
    a skipped prune costs disk, this costs data."""
    async def go(loop):
        store = store_factory(loop, retention_days=0)
        store.free_retention_days = 30
        store.set_subscribers(set(), ledger_ok=False)       # what live_subscribers returns on failure
        await _seed(store)
        before = await store.count()
        preview = await store.prune_preview()
        removed = await store.prune()
        assert removed == 0 and preview["total"] == 0
        assert await store.count() == before
        assert preview["tiered_ok"] is False
    _run(go)


def test_zero_free_days_keeps_everything_even_with_the_feature_on(store_factory):
    """Enabling the feature must not start deleting on its own — the window is a separate, explicit
    number, and 0 keeps today's behaviour."""
    async def go(loop):
        store = store_factory(loop, retention_days=0)
        store.free_retention_days = 0
        store.paid_retention_days = 30
        store.set_subscribers([SUBSCRIBER], ledger_ok=True)
        await _seed(store)
        before = await store.count()
        assert await store.prune() == 0
        assert await store.count() == before
    _run(go)


def test_profiles_contacts_and_dms_are_never_tiered(store_factory):
    """The tier only ever touches high-volume feed kinds. A stranger's profile, contact list, relay
    list and DMs are not storage this feature sells — losing them would break their account."""
    async def go(loop):
        store = store_factory(loop, retention_days=0)
        store.free_retention_days = 30
        store.set_subscribers(set(), ledger_ok=True)
        keep = [_ev(900 + i, pubkey=STRANGER, age_days=400, kind=k)
                for i, k in enumerate((0, 3, 4, 1059, 10002, 30078))]
        await store.add_events_bulk(keep + [_ev(950, pubkey=STRANGER, age_days=400)],
                                    origin="direct")
        removed = await store.prune()
        assert removed == 1, "only the kind-1 note"
        assert await store.count() == len(keep)
    _run(go)


def test_a_lapsed_subscriber_falls_back_to_the_free_window(store_factory):
    """What "pay to stay" means when you stop paying — pinned because the alternative (an expired
    row still counted as a subscriber) would make the tier unenforceable."""
    async def go(loop):
        store = store_factory(loop, retention_days=0)
        store.free_retention_days = 30
        await _seed(store)
        expired = {"updated": 1, "cursor": 0, "credited": {},
                   "subs": {SUBSCRIBER: {"until": int(time.time()) - DAY}}}
        live = {pk for pk, rec in prs._normalize(expired)["subs"].items()
                if rec["until"] > int(time.time())}
        assert live == set(), "an expired subscription is not a live one"
        store.set_subscribers(live, ledger_ok=True)
        removed = await store.prune()
        assert removed == 90, "everyone's old notes, the lapsed subscriber's included"
    _run(go)


# ---- what a visitor is told --------------------------------------------

def _fake_policy(**kw):
    base = {"enabled": True, "free_days": 30, "paid_days": 0, "sats_per_month": 2000,
            "lud16": "relay@example.com", "pubkey": "ab" * 32}
    base.update(kw)
    return lambda: base


def test_the_splash_page_says_nothing_unless_the_feature_is_on(monkeypatch):
    """Every node renders this page. A relay that doesn't charge must not grow a price list, and one
    with no free window has nothing to sell — its visitors' posts are already kept forever."""
    from app.services.nostr_relay import server as srv
    srv._retention_cache.update(key=None, html="", at=0.0)
    monkeypatch.setattr(prs, "policy", _fake_policy(enabled=False))
    assert srv._retention_block() == ""
    srv._retention_cache.update(key=None, html="", at=0.0)
    monkeypatch.setattr(prs, "policy", _fake_policy(free_days=0))
    assert srv._retention_block() == ""


def test_the_splash_page_offers_a_scannable_profile_not_a_payment(monkeypatch):
    """The QR must encode the `nostr:` profile to ZAP, never the lightning address: only a zap
    carries the payer's identity, so a plain-payment QR would take sats and credit nobody."""
    from app.services.nostr_relay import server as srv
    srv._retention_cache.update(key=None, html="", at=0.0)
    monkeypatch.setattr(prs, "policy", _fake_policy())
    html = srv._retention_block()
    assert "30 days" in html and "2000 sats / month" in html
    assert "<svg" in html, "the QR should render (segno is a hard dependency)"
    assert "lightning:" not in html, "a scannable payment URI here would be uncreditable"
    assert "relay@example.com" in html, "the destination is still shown, as text to verify against"
    assert "npub1" in html


def test_nip11_advertises_the_policy_only_when_it_is_real(monkeypatch):
    import json as _json
    import types
    from app.services.nostr_relay.server import RelayServer
    fake = types.SimpleNamespace(cfg={"name": "R", "description": "d"})
    monkeypatch.setattr(prs, "policy", _fake_policy(enabled=False))
    off = _json.loads(RelayServer.nip11_doc(fake))
    assert "retention" not in off and "fees" not in off
    monkeypatch.setattr(prs, "policy", _fake_policy())
    on = _json.loads(RelayServer.nip11_doc(fake))
    assert on["retention"][0]["time"] == 30 * DAY
    assert 1 in on["retention"][0]["kinds"] and on["retention"][1]["time"] is None
    assert on["fees"]["subscription"][0] == {"amount": 2_000_000, "unit": "msats", "period": 2592000}


# ---- the settings themselves -------------------------------------------

SETTING_KEYS = ("nostr_relay_paid_retention_enabled", "nostr_relay_free_retention_days",
                "nostr_relay_paid_retention_days", "nostr_relay_paid_sats_per_month",
                "nostr_relay_paid_lud16", "nostr_relay_paid_pubkey")


@pytest.mark.parametrize("key", SETTING_KEYS)
def test_every_setting_is_declared_so_it_hydrates(key):
    """A key missing from SettingsResponse is DROPPED from the GET, so its input loads blank on every
    visit and the checkbox then posts `false` over the stored value on the next Save — silently
    turning the feature off. That has bitten this repo four times."""
    from app.schemas import SettingsResponse
    assert key in SettingsResponse.model_fields


@pytest.mark.parametrize("key", SETTING_KEYS)
def test_every_setting_persists_to_the_relay(key):
    """These are shareable settings, not per-node plumbing: they must reach the operator-signed
    `pcai:setting:<key>` doc and hydrate back from it, not live only in local_settings.json."""
    from app.services import settings_store
    assert not settings_store._is_local_only(key)


@pytest.mark.parametrize("key", SETTING_KEYS)
def test_every_setting_has_an_admin_input_whose_id_matches_its_name(key):
    """Hydration reads the id, Save reads the name — a mismatch loads or saves nothing."""
    import pathlib
    import re
    html = pathlib.Path("templates/admin/tabs/nostr_relay.html").read_text()
    m = re.search(rf'<input[^>]*\bname="{key}"[^>]*>', html)
    assert m, f"no admin input for {key}"
    assert f'id="{key}"' in m.group(0), f"{key}: id must equal name"


def test_the_shipped_defaults_are_inert():
    """Every node deploys with this off and deleting nothing. Deliberately NOT seeded into
    database.py's default_settings — seeding would publish six documents to every relay for a
    feature nobody enabled, and the schema default is already the off state."""
    from app.schemas import SettingsResponse
    f = SettingsResponse.model_fields
    assert f["nostr_relay_paid_retention_enabled"].default is False
    assert f["nostr_relay_free_retention_days"].default == 0
    assert f["nostr_relay_paid_retention_days"].default == 0
    assert f["nostr_relay_paid_sats_per_month"].default == 0


# ---- the exemption on ORDINARY auto-clean (synced content) -------------

async def _seed_synced(store):
    """The same corpus, but arrived over the firehose (origin='wot') — what the pre-existing age
    prune and count cap actually operate on."""
    evs = []
    n = 0
    for pk in (STRANGER, SUBSCRIBER, LOCAL):
        for age in (90, 1):
            for _ in range(30):
                n += 1
                evs.append(_ev(5000 + n, pubkey=pk, age_days=age))
    await store.add_events_bulk(evs, origin="wot")


def test_a_subscriber_is_spared_the_ordinary_age_prune(store_factory):
    """A subscriber pays for their posts to stay on this relay. Which relay the copy we hold arrived
    from is our implementation detail, not something they bought a different answer for."""
    async def go(loop):
        store = store_factory(loop, retention_days=30)
        store.set_paid_tier_enabled(True)
        store.set_subscribers([SUBSCRIBER], ledger_ok=True)
        await _seed_synced(store)
        preview = await store.prune_preview()
        removed = await store.prune()
        assert preview["aged"] == removed == 60, "the stranger's and the local user's 30 each"
        left = {pk: len(await store.query([{"authors": [pk], "limit": 500}]))
                for pk in (STRANGER, SUBSCRIBER, LOCAL)}
        assert left == {STRANGER: 30, SUBSCRIBER: 60, LOCAL: 30}
    _run(go)


def test_with_the_feature_off_nobody_is_exempt(store_factory):
    """The exemption must not be reachable on a node that never enabled pay-to-stay — including via
    a subscriber set left in memory from before the switch was turned off."""
    async def go(loop):
        store = store_factory(loop, retention_days=30)
        store.set_paid_tier_enabled(True)
        store.set_subscribers([SUBSCRIBER], ledger_ok=True)
        store.set_paid_tier_enabled(False)          # admin turns the whole feature off
        await _seed_synced(store)
        assert store._subscriber_exempt() == ""
        assert await store.prune() == 90, "every author's old synced notes, subscriber included"
    _run(go)


def test_a_hiccup_reading_the_ledger_does_not_expose_a_subscriber(store_factory):
    """Deliberately NOT the fail-closed behaviour the tiered rules use: this rule is the relay's only
    bound on firehose growth, so it keeps running — but it falls back to the last set it read rather
    than to 'nobody has paid'."""
    async def go(loop):
        store = store_factory(loop, retention_days=30)
        store.set_paid_tier_enabled(True)
        store.set_subscribers([SUBSCRIBER], ledger_ok=True)     # a good read
        store.set_subscribers(set(), ledger_ok=False)           # …then the relay hiccups
        await _seed_synced(store)
        removed = await store.prune()
        assert removed == 60, "the age prune still runs — but not over the subscriber"
        assert len(await store.query([{"authors": [SUBSCRIBER], "limit": 500}])) == 60
    _run(go)


def test_a_real_read_showing_a_lapse_removes_the_exemption(store_factory):
    """The last-known-good fallback must not ossify: a SUCCESSFUL read that no longer lists someone
    is the subscription ending, and their synced notes age out like anyone else's."""
    async def go(loop):
        store = store_factory(loop, retention_days=30)
        store.set_paid_tier_enabled(True)
        store.set_subscribers([SUBSCRIBER], ledger_ok=True)
        store.set_subscribers(set(), ledger_ok=True)            # subscription lapsed
        await _seed_synced(store)
        assert await store.prune() == 90
    _run(go)


def test_the_count_cap_spares_a_subscriber_too(store_factory):
    """The cap already spares preserved authors and every direct write; a paying author is not the
    one to treat more harshly than either."""
    async def go(loop):
        store = store_factory(loop, retention_days=0, max_events=10)
        store.set_paid_tier_enabled(True)
        store.set_subscribers([SUBSCRIBER], ledger_ok=True)
        await _seed_synced(store)
        preview = await store.prune_preview()
        removed = await store.prune()
        assert preview["capped"] == removed
        assert len(await store.query([{"authors": [SUBSCRIBER], "limit": 500}])) == 60
    _run(go)


# ---- who gets told what ------------------------------------------------

class _Ledger:
    """A stand-in for the relay-backed ledger: load/save in memory so the notification rules can be
    driven without a relay. Mirrors the real contract — load raises on failure, returns None when the
    document doesn't exist."""

    def __init__(self, doc=None, fail=False, refuse_write=False):
        self.doc, self.fail, self.refuse_write, self.writes = doc, fail, refuse_write, 0

    async def load(self):
        if self.fail:
            raise RuntimeError("relay unreachable")
        return None if self.doc is None else prs._normalize(self.doc)

    async def save(self, ledger):
        if self.refuse_write:
            return False
        self.writes += 1
        self.doc = json.loads(json.dumps(ledger))
        return True


def _wire(monkeypatch, led, *, free_days=30):
    sent = []
    monkeypatch.setattr(prs, "load_ledger", led.load)
    monkeypatch.setattr(prs, "save_ledger", led.save)
    monkeypatch.setattr(prs, "enabled", lambda: True)
    monkeypatch.setattr(prs, "policy", _fake_policy(free_days=free_days))

    async def _dm(pk, text):
        sent.append((pk, text))
        return True
    monkeypatch.setattr(prs, "_dm", _dm)
    return sent


def _sub(**kw):
    rec = {"until": 0, "msats": 0, "bal": 0, "since": 0, "warned": 0, "ended": 0}
    rec.update(kw)
    return {"updated": 1, "cursor": 1, "credited": {}, "subs": {"a" * 64: rec}}


def test_a_subscriber_is_warned_before_they_lapse(monkeypatch):
    """The DM that actually prevents a loss: after the lapse, the free window applies and the next
    auto-clean takes their older posts. Finding that out afterwards is finding out too late."""
    until = int(time.time()) + 3 * DAY
    led = _Ledger(_sub(until=until))
    sent = _wire(monkeypatch, led)
    assert asyncio.run(prs.notify_lifecycle()) == 1
    pk, text = sent[0]
    assert pk == "a" * 64 and "ends on" in text and "30 days" in text


def test_nobody_is_warned_twice(monkeypatch):
    """It ticks every 5 minutes. A marker that didn't persist — or that `_normalize` dropped on the
    next read — would DM the same person 288 times a day."""
    led = _Ledger(_sub(until=int(time.time()) + 3 * DAY))
    sent = _wire(monkeypatch, led)
    assert asyncio.run(prs.notify_lifecycle()) == 1
    assert asyncio.run(prs.notify_lifecycle()) == 0, "the marker must survive a normalize round trip"
    assert len(sent) == 1


def test_renewing_re_arms_the_warning(monkeypatch):
    led = _Ledger(_sub(until=int(time.time()) + 3 * DAY))
    sent = _wire(monkeypatch, led)
    asyncio.run(prs.notify_lifecycle())
    # …they renew: a new expiry, so the marker no longer matches and the next lapse warns again.
    led.doc["subs"]["a" * 64]["until"] = int(time.time()) + 2 * DAY
    assert asyncio.run(prs.notify_lifecycle()) == 1
    assert len(sent) == 2


def test_the_end_is_announced_once_and_says_what_happens_next(monkeypatch):
    led = _Ledger(_sub(until=int(time.time()) - DAY))
    sent = _wire(monkeypatch, led)
    assert asyncio.run(prs.notify_lifecycle()) == 1
    assert "ended" in sent[0][1] and "30-day window" in sent[0][1]
    assert asyncio.run(prs.notify_lifecycle()) == 0


def test_a_ledger_that_cannot_be_read_sends_nothing(monkeypatch):
    """Unreadable, or not there at all: there is nothing to notify anyone about."""
    sent = _wire(monkeypatch, _Ledger(fail=True))
    assert asyncio.run(prs.notify_lifecycle()) == 0 and sent == []
    sent = _wire(monkeypatch, _Ledger(None))
    assert asyncio.run(prs.notify_lifecycle()) == 0 and sent == []


def test_a_refused_marker_write_still_warns_and_accepts_the_repeat(monkeypatch):
    """The deliberate opposite of the PAYMENT path, and the asymmetry is principled: a payment DM
    asserts persisted state, so it must not be sent unless the credit was saved. A lapse warning
    asserts a fact about the clock, which is true whether or not the marker saved — so it goes out,
    and a repeat is accepted as the price of never missing it."""
    led = _Ledger(_sub(until=int(time.time()) + 2 * DAY), refuse_write=True)
    sent = _wire(monkeypatch, led)
    assert asyncio.run(prs.notify_lifecycle()) == 1
    assert len(sent) == 1
    assert asyncio.run(prs.notify_lifecycle()) == 1, "unsaved marker → it repeats, visibly, in the log"


def test_lifecycle_says_nothing_while_the_feature_is_off(monkeypatch):
    led = _Ledger(_sub(until=int(time.time()) - DAY))
    sent = _wire(monkeypatch, led)
    monkeypatch.setattr(prs, "enabled", lambda: False)
    assert asyncio.run(prs.notify_lifecycle()) == 0 and sent == []


def test_an_admin_grant_tells_the_recipient(monkeypatch):
    led = _Ledger(_sub(until=0))
    sent = _wire(monkeypatch, led)
    rec = asyncio.run(prs.grant("b" * 64, 30))
    assert rec["until"] > int(time.time())
    assert len(sent) == 1 and "extended by 30" in sent[0][1]


def test_taking_days_back_does_not_congratulate_anyone(monkeypatch):
    led = _Ledger(_sub(until=int(time.time()) + 30 * DAY))
    sent = _wire(monkeypatch, led)
    asyncio.run(prs.grant("a" * 64, -30))
    assert sent == [], "a revocation is not an extension"


# ---- the credit loop, end to end ---------------------------------------

def _wire_scan(monkeypatch, led, receipts, zap_pub, recv_pub, *, price_sats=1000):
    """Drive scan_once with a stubbed relay + LNURL + ledger. Returns (payer DMs, admin messages)."""
    from app.services import settings_store
    from app.services.nostr import relay as relay_client
    sent = _wire(monkeypatch, led)
    admin = []
    monkeypatch.setattr(prs, "receiving_pubkey", lambda: recv_pub)
    monkeypatch.setattr(prs, "price_msats_per_month", lambda: price_sats * 1000)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: "relay@example.com" if "lud16" in k else (d or ""))

    async def _lnurl(addr):
        return {"allowsNostr": True, "nostrPubkey": zap_pub}
    monkeypatch.setattr(prs, "_lnurl_params", _lnurl)

    async def _query(relays, filters, **kw):
        return list(receipts)
    monkeypatch.setattr(relay_client, "query", _query)

    async def _adm(text):
        admin.append(text)
    monkeypatch.setattr(prs, "_tell_admin", _adm)
    return sent, admin


def _fresh_ledger():
    return _Ledger({"updated": 1, "cursor": 1, "credited": {}, "subs": {}})


def test_a_zap_buys_days_and_the_admin_hears_about_it(monkeypatch):
    zap_sk, zap_pub = _key()
    payer_sk, payer_pub = _key()
    _, recv_pub = _key()
    ev, _ = _receipt(zap_sk, payer_sk, recv_pub, msats=2_000_000)      # 2000 sats = 2 months
    led = _fresh_ledger()
    sent, admin = _wire_scan(monkeypatch, led, [ev], zap_pub, recv_pub, price_sats=1000)

    assert asyncio.run(prs.scan_once()) == 60
    assert led.doc["subs"][payer_pub]["until"] >= int(time.time()) + 59 * DAY
    assert len(sent) == 1 and "extended by 60 day(s)" in sent[0][1]
    assert len(admin) == 1 and "2000 sats" in admin[0]


def test_the_same_receipt_is_never_credited_twice(monkeypatch):
    """Scans overlap on purpose (receipts arrive out of order), so the same event WILL be seen again.
    Crediting it twice would hand out storage for free, forever."""
    zap_sk, zap_pub = _key()
    payer_sk, payer_pub = _key()
    _, recv_pub = _key()
    ev, _ = _receipt(zap_sk, payer_sk, recv_pub, msats=1_000_000)
    led = _fresh_ledger()
    _wire_scan(monkeypatch, led, [ev], zap_pub, recv_pub, price_sats=1000)

    first = asyncio.run(prs.scan_once())
    until = led.doc["subs"][payer_pub]["until"]
    assert asyncio.run(prs.scan_once()) == 0, "the second sighting must credit nothing"
    assert led.doc["subs"][payer_pub]["until"] == until
    assert first == 30


def test_small_zaps_add_up_instead_of_rounding_to_nothing(monkeypatch):
    """Two half-price zaps must buy what one full-price zap does — and the first one, which bought no
    whole day, still gets an answer instead of silence."""
    zap_sk, zap_pub = _key()
    payer_sk, payer_pub = _key()
    _, recv_pub = _key()
    led = _fresh_ledger()
    half, _ = _receipt(zap_sk, payer_sk, recv_pub, msats=15_000)       # 15 sats @ 1000/mo → 0 days
    sent, _adm = _wire_scan(monkeypatch, led, [half], zap_pub, recv_pub, price_sats=1000)
    assert asyncio.run(prs.scan_once()) == 0
    assert led.doc["subs"][payer_pub]["bal"] == 15_000
    assert len(sent) == 1 and "banked" in sent[0][1], "a payment that bought no day still gets a reply"

    other, _ = _receipt(zap_sk, payer_sk, recv_pub, msats=20_000)      # …now over a day's worth
    _wire_scan(monkeypatch, led, [other], zap_pub, recv_pub, price_sats=1000)
    assert asyncio.run(prs.scan_once()) == 1
    assert led.doc["subs"][payer_pub]["bal"] < 34_000, "the spent balance must be consumed"


def test_a_forged_receipt_in_the_batch_credits_nobody(monkeypatch):
    """The verified and the forged arrive on the same query. One must land, the other must not."""
    zap_sk, zap_pub = _key()
    good_sk, good_pub = _key()
    liar_sk, liar_pub = _key()
    _, recv_pub = _key()
    real, _ = _receipt(zap_sk, good_sk, recv_pub, msats=1_000_000)
    fake, _ = _receipt(liar_sk, liar_sk, recv_pub, msats=100_000_000)   # self-signed, not the zapper
    led = _fresh_ledger()
    _wire_scan(monkeypatch, led, [real, fake], zap_pub, recv_pub, price_sats=1000)
    assert asyncio.run(prs.scan_once()) == 30
    assert good_pub in led.doc["subs"] and liar_pub not in led.doc["subs"]


def test_a_refused_ledger_write_credits_nothing_and_promises_nothing(monkeypatch):
    """If the write didn't land, the credit doesn't exist — so no DM may claim it did."""
    zap_sk, zap_pub = _key()
    payer_sk, _ = _key()
    _, recv_pub = _key()
    ev, _ = _receipt(zap_sk, payer_sk, recv_pub, msats=1_000_000)
    led = _fresh_ledger()
    led.refuse_write = True
    sent, admin = _wire_scan(monkeypatch, led, [ev], zap_pub, recv_pub, price_sats=1000)
    assert asyncio.run(prs.scan_once()) == 0
    assert sent == [] and admin == []


def test_enabling_the_feature_does_not_credit_historical_tips(monkeypatch):
    """First run creates the ledger with the cursor at NOW. Otherwise every tip the operator ever
    received would retroactively buy storage nobody sold."""
    zap_sk, zap_pub = _key()
    payer_sk, _ = _key()
    _, recv_pub = _key()
    ev, _ = _receipt(zap_sk, payer_sk, recv_pub, msats=9_000_000)
    led = _Ledger(None)                                   # no ledger document yet
    sent, _adm = _wire_scan(monkeypatch, led, [ev], zap_pub, recv_pub, price_sats=1000)
    assert asyncio.run(prs.scan_once()) == 0
    assert led.doc["subs"] == {} and led.doc["cursor"] >= int(time.time()) - 5
    assert sent == []


def test_a_failed_dm_leaves_the_warning_to_be_retried(monkeypatch):
    """The marker is written only for a DM that went out. Marking first would let one transient
    publish failure swallow a subscriber's only warning before their posts are deleted — a repeated
    DM is an annoyance, a missed one is the failure this exists to prevent."""
    led = _Ledger(_sub(until=int(time.time()) + 3 * DAY))
    _wire(monkeypatch, led)
    fail = {"on": True}

    async def _dm(pk, text):
        return not fail["on"]
    monkeypatch.setattr(prs, "_dm", _dm)

    assert asyncio.run(prs.notify_lifecycle()) == 0
    assert led.doc["subs"]["a" * 64]["warned"] == 0, "a DM that failed must not be marked as sent"
    fail["on"] = False
    assert asyncio.run(prs.notify_lifecycle()) == 1, "the retry gets through"


def test_an_ending_from_long_ago_is_not_announced(monkeypatch):
    """A ledger written before this code existed, or a worker down for a week, must not blast a
    backlog of 'your subscription ended' DMs about endings nobody needs told about now."""
    led = _Ledger(_sub(until=int(time.time()) - 90 * DAY))
    sent = _wire(monkeypatch, led)
    assert asyncio.run(prs.notify_lifecycle()) == 0 and sent == []


def test_a_renewal_landing_mid_flight_does_not_get_a_stale_marker(monkeypatch):
    """The DM is sent, then the marker is written against a re-read ledger. If a renewal changed the
    expiry in between, marking it would suppress the warning for the NEW expiry too."""
    until = int(time.time()) + 3 * DAY
    led = _Ledger(_sub(until=until))
    _wire(monkeypatch, led)

    async def _dm(pk, text):
        led.doc["subs"]["a" * 64]["until"] = until + 300 * DAY      # they renew mid-flight
        return True
    monkeypatch.setattr(prs, "_dm", _dm)

    assert asyncio.run(prs.notify_lifecycle()) == 1
    assert led.doc["subs"]["a" * 64]["warned"] == 0, "the marker belonged to an expiry that's gone"
