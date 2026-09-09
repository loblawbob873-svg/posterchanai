"""A user's own Monero history renders, and unconfirmed INCOMING money points the right way.

Both wallets get the same get_transfers buckets, so they flatten through one helper. That helper
also fixes a real defect in the node wallet's own display: buckets were carried straight through as
the direction, and `transferView` treats only 'in' as incoming — so `pool`, which is unconfirmed
money ARRIVING, rendered as "Sent ↑". That is exactly the window somebody looks in after being told
a payment was sent.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/monero-wallet.js").read_text(encoding="utf-8")


def _run(js):
    body = SRC[SRC.index("  function transferView("):]
    body = body[:body.index("\n  function paint(")]
    script = ("const esc=s=>String(s);const xmr=(v)=>String(v);\n"
              "function historyDate(){return 'when';}\n" + body + "\n" + js)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_incoming_unconfirmed_is_not_shown_as_sent():
    got = _run("console.log(JSON.stringify(flattenTransfers({pool:[{txid:'a',amount:1}]})));")
    assert '"direction":"in"' in got, got
    assert '"unconfirmed":true' in got, got


def test_outgoing_unconfirmed_stays_outgoing():
    got = _run("console.log(JSON.stringify(flattenTransfers({pending:[{txid:'b',amount:1}]})));")
    assert '"direction":"out"' in got and '"unconfirmed":true' in got, got


def test_confirmed_directions_are_preserved():
    got = _run("console.log(JSON.stringify(flattenTransfers({"
               "'in':[{txid:'c',timestamp:2}],out:[{txid:'d',timestamp:1}]})));")
    assert got.index('"txid":"c"') < got.index('"txid":"d"'), "not sorted newest first"
    assert '"unconfirmed":false' in got


def test_a_failed_history_request_is_not_read_as_no_transactions():
    assert _run("console.log(JSON.stringify(flattenTransfers({__error:'boom'})));").strip() == "[]"
    assert "history_unavailable" in SRC, "a failed history is not distinguished from an empty one"
    assert "could not be loaded just now" in SRC


def test_the_pool_row_renders_as_received():
    got = _run("console.log(transferRows(flattenTransfers({pool:[{txid:'a',amount:1}]})));")
    assert "Received" in got and "↓" in got, got


def test_the_user_wallet_asks_for_its_own_history():
    assert "/api/wallet/xmr/me/history?limit=50" in SRC
    # It must not decide whether the wallet is usable.
    probe = SRC[SRC.index("async function meProbe("):SRC.index("async function meProbe(") + 1800]
    assert ".catch(" in probe, "a failed history request would break the whole wallet probe"


def test_both_wallets_use_one_flattener():
    assert SRC.count("function flattenTransfers(") == 1
    assert SRC.count("for(const kind of ['in','out','pending','failed','pool'])") == 1, \
        "a second copy of the bucket rule has appeared"
