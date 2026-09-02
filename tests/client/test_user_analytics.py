"""My Analytics is reachable everywhere and its event accounting is deterministic."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_analytics_is_a_registered_offline_module():
    html = (ROOT / "templates/client.html").read_text()
    app = (ROOT / "static/js/client/app.js").read_text()
    sw = (ROOT / "static/js/client/sw.js").read_text()
    assert 'data-view="analytics"' in html
    assert "renderModuleView('analytics','user-analytics.js','PCUserAnalytics','render')" in app
    assert "analytics:'My Analytics" in app
    assert "'/static/js/client/user-analytics.js'" in sw


def test_analytics_counts_dedupes_targets_and_ranks_posts():
    js = r"""
global.window={__PC:{enc:String,isView:()=>false}};
global.document={};
require('./static/js/client/user-analytics.js');
const now=2000000000, a={id:'a',kind:1,created_at:now-100,content:'A'}, b={id:'b',kind:1,created_at:now-200,content:'B'};
const ev=(id,kind,target,age,tags=[])=>({id,kind,created_at:now-age,tags:[['e',target,'','root'],...tags]});
const events=[ev('r1',7,'a',10),ev('r1',7,'a',10),ev('q1',1,'a',20),ev('x1',6,'b',30),ev('z1',9735,'a',40,[['amount','21000']])];
const out=window.PCUserAnalytics._compute([a,b,a],events,now);
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(["node", "-e", js], cwd=ROOT, text=True, capture_output=True, check=True)
    out = json.loads(proc.stdout)
    assert out["totals"] == {"posts": 2, "replies": 1, "reactions": 1, "reposts": 1, "zaps": 21,
                             "xmr": 0, "bch": 0, "tips": 0, "engagement": 3, "rate": 1.5}
    assert out["top"][0]["post"]["id"] == "a"
    assert sum(day["posts"] for day in out["daily"]) == 2
    assert sum(day["engagement"] for day in out["daily"]) == 4


def test_analytics_mobile_layout_and_accessible_chart_label_ship():
    css = (ROOT / "static/css/client.css").read_text()
    js = (ROOT / "static/js/client/user-analytics.js").read_text()
    assert "@media(max-width:700px)" in css
    assert ".ua-chartgrid{grid-template-columns:1fr}" in css
    assert 'aria-label="${range} day ${key} chart"' in js
    assert "Couldn’t load analytics from your relays" in js


# ── Monero (and Bitcoin Cash) tips ───────────────────────────────────────────────────────────────
#
# An address tip is published as a KIND 1 note carrying `t:monerotip` and `amount_xmr` — there is no
# zap receipt for a chain the sender paid from their own wallet — so it arrives through exactly the
# same `#e` query as a reply. Before this, every Monero tip anybody sent inflated the reply count
# and appeared nowhere as support received.

def compute(posts, events, now, days):
    """Run the SHIPPED module under node, the way the rest of this file does."""
    js = ("global.window={__PC:{enc:String,isView:()=>false}};global.document={};"
          "require('./static/js/client/user-analytics.js');"
          f"process.stdout.write(JSON.stringify(window.PCUserAnalytics._compute("
          f"{json.dumps(posts)},{json.dumps(events)},{now},{days})));")
    proc = subprocess.run(["node", "-e", js], cwd=ROOT, text=True, capture_output=True, check=True)
    return json.loads(proc.stdout)


def _tip(target_id, kind_tag, amount_tag, amount, at=1000):
    return {"id": f"tip{amount_tag}{amount}", "kind": 1, "created_at": at,
            "tags": [["e", target_id], ["t", kind_tag], [amount_tag, str(amount)]]}


def test_a_monero_tip_is_support_not_a_reply():
    post = {"id": "p1", "kind": 1, "created_at": 900, "content": "hi", "tags": []}
    got = compute([post], [_tip("p1", "monerotip", "amount_xmr", "0.01")], 1000, 30)
    assert got["totals"]["replies"] == 0, "a Monero tip is still being counted as a reply"
    assert got["totals"]["tips"] == 1
    assert abs(got["totals"]["xmr"] - 0.01) < 1e-12


def test_the_xmr_amount_is_a_decimal_not_an_integer():
    """0.0002 XMR must not become 0. Chain amounts are decimals; sats are not."""
    post = {"id": "p1", "kind": 1, "created_at": 900, "content": "hi", "tags": []}
    got = compute([post], [_tip("p1", "monerotip", "amount_xmr", "0.0002")], 1000, 30)
    assert got["totals"]["xmr"] > 0


def test_xmr_is_never_added_to_the_sats_total():
    """Different units. 0.01 XMR added to a sats figure would be nonsense in both directions."""
    post = {"id": "p1", "kind": 1, "created_at": 900, "content": "hi", "tags": []}
    got = compute([post], [_tip("p1", "monerotip", "amount_xmr", "0.01")], 1000, 30)
    assert got["totals"]["zaps"] == 0


def test_a_bitcoin_cash_tip_counts_too():
    post = {"id": "p1", "kind": 1, "created_at": 900, "content": "hi", "tags": []}
    got = compute([post], [_tip("p1", "bchtip", "amount_bch", "0.5")], 1000, 30)
    assert got["totals"]["replies"] == 0 and got["totals"]["tips"] == 1
    assert abs(got["totals"]["bch"] - 0.5) < 1e-12


def test_an_ordinary_reply_is_still_a_reply():
    """The discriminator is the `t` tag and nothing else — a normal kind-1 reply must be unaffected."""
    post = {"id": "p1", "kind": 1, "created_at": 900, "content": "hi", "tags": []}
    reply = {"id": "r1", "kind": 1, "created_at": 950, "tags": [["e", "p1"]]}
    got = compute([post], [reply], 1000, 30)
    assert got["totals"]["replies"] == 1 and got["totals"]["tips"] == 0


def test_a_tip_with_no_amount_still_counts_as_support():
    """Somebody tipped; we just do not know how much. Dropping it would under-report the act."""
    post = {"id": "p1", "kind": 1, "created_at": 900, "content": "hi", "tags": []}
    tip = {"id": "t0", "kind": 1, "created_at": 960, "tags": [["e", "p1"], ["t", "monerotip"]]}
    got = compute([post], [tip], 1000, 30)
    assert got["totals"]["tips"] == 1 and got["totals"]["xmr"] == 0
    assert got["totals"]["replies"] == 0


def test_tips_count_towards_engagement():
    post = {"id": "p1", "kind": 1, "created_at": 900, "content": "hi", "tags": []}
    got = compute([post], [_tip("p1", "monerotip", "amount_xmr", "0.01")], 1000, 30)
    assert got["totals"]["engagement"] == 1, "a tip is engagement — it was invisible before"


def test_the_view_shows_a_monero_card():
    """The number is worth nothing if it is never drawn. Shown unconditionally, so a node that has
    received none reads '0 XMR' rather than leaving the user wondering whether it is counted."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / "static/js/client/user-analytics.js").read_text(encoding="utf-8")
    assert "Monero tips" in src and "XMR</em>" in src.replace(" ", "")or "XMR" in src
    assert "coin(t.xmr)" in src, "the card does not render the XMR total"
