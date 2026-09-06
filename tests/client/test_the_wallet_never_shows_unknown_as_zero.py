"""A BALANCE NOBODY COULD FETCH MUST NOT RENDER AS 0.

The server answers `known` per chain precisely so the screen can tell those apart, and a client that
reads `amount` without checking it prints a confident "0" for a chain whose provider was down. Zero
and unknown look identical as digits and mean opposite things: one says the provider is unreachable,
the other says your coins are gone. Somebody who sees 0.00 BTC concludes they have been robbed;
somebody who sees it before sending concludes they can afford nothing.

The shipped row renderer RUNS here, because the question is what the HTML says — and both the buggy
version and the correct one read the same field names, so matching the source proves nothing.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "static/js/client/exodus.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _render(cell, sym='BTC', name='Bitcoin'):
    js = textwrap.dedent(f"""
        const fs=require('fs'), vm=require('vm');
        const ctx={{ window:{{}}, globalThis:{{}}, setTimeout, console, document:{{querySelector:()=>null,
                     querySelectorAll:()=>[]}} }};
        ctx.window=ctx; ctx.globalThis=ctx;
        ctx.window.__PC={{ enc:(s)=>String(s==null?'':s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;',
                            '>':'&gt;','"':'&quot;'}})[c]) }};
        vm.createContext(ctx);
        vm.runInContext(fs.readFileSync({json.dumps(str(MOD))},'utf8'), ctx);
        const html = ctx.PCExodus._row({json.dumps(sym)},{json.dumps(name)}, {json.dumps(cell)});
        process.stdout.write(JSON.stringify({{html}}));
    """)
    out = subprocess.run(["node", "-e", js], cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr[:2000]
    return json.loads(out.stdout)["html"]


def test_an_unreachable_chain_says_unavailable_and_never_prints_a_number():
    html = _render({"known": False, "units": None, "amount": None, "address": "1abc"})
    assert "unavailable" in html
    assert ">0<" not in html and ">0.0" not in html, html
    assert "not a zero balance" in html, "the tooltip that explains it is gone"


def test_a_real_zero_is_shown_as_a_number():
    """The other half: a chain that genuinely holds nothing must say 0, not 'unavailable'."""
    html = _render({"known": True, "units": 0, "amount": "0", "address": "1abc"})
    assert "unavailable" not in html
    assert "<b>0</b>" in html, html


def test_a_real_balance_is_shown_exactly_as_the_server_said():
    """No re-formatting on this side. The server did the decimal maths with Decimal; a JS number
    would round it, and this is money."""
    html = _render({"known": True, "units": 12345678, "amount": "0.12345678", "address": "1abc"})
    assert "0.12345678" in html


def test_a_missing_cell_is_unavailable_rather_than_zero():
    """A chain the balances call did not mention at all -- the row still has to exist, because a
    missing row reads as 'you don't have that coin'."""
    for cell in (None, {}, {"known": None}):
        assert "unavailable" in _render(cell)


def test_the_custody_line_is_not_hidden_in_a_help_page():
    js = textwrap.dedent(f"""
        const fs=require('fs'), vm=require('vm');
        const ctx={{ window:{{}}, setTimeout, console, document:{{querySelector:()=>null}} }};
        ctx.window=ctx; ctx.globalThis=ctx; ctx.window.__PC={{enc:(s)=>String(s)}};
        vm.createContext(ctx);
        vm.runInContext(fs.readFileSync({json.dumps(str(MOD))},'utf8'), ctx);
        process.stdout.write(JSON.stringify({{html: ctx.PCExodus._custodyNote()}}));
    """)
    out = subprocess.run(["node", "-e", js], cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr[:2000]
    # Whitespace-normalised: the copy wraps across lines in the template literal, and a test that
    # breaks when somebody re-wraps a sentence is a test nobody keeps.
    html = " ".join(json.loads(out.stdout)["html"].split()).lower()
    assert "node holds the keys" in html
    assert "can spend this wallet" in html
    assert "hot wallet" in html


# ── which chains offer a Send button ───────────────────────────────────────────────────────────
#
# Receiving works on every chain; spending is implemented for the EVM chains only. The button is
# ABSENT on the others rather than present-and-refusing: a control that always says no is a worse
# answer than no control. The server refuses them too — a button the page hides is not a rule.

def _can_send(sym):
    js = textwrap.dedent(f"""
        const fs=require('fs'), vm=require('vm');
        const ctx={{ window:{{}}, setTimeout, console, document:{{querySelector:()=>null}} }};
        ctx.window=ctx; ctx.globalThis=ctx; ctx.window.__PC={{enc:(s)=>String(s)}};
        vm.createContext(ctx);
        vm.runInContext(fs.readFileSync({json.dumps(str(MOD))},'utf8'), ctx);
        process.stdout.write(JSON.stringify({{can: ctx.PCExodus._canSend({json.dumps(sym)})}}));
    """)
    out = subprocess.run(["node", "-e", js], cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr[:2000]
    return json.loads(out.stdout)["can"]


def test_the_evm_chains_offer_send():
    for sym in ("ETH", "MATIC", "BNB", "AVAX"):
        assert _can_send(sym) is True, sym


def test_the_chains_that_cannot_send_show_no_send_button():
    for sym in ("BTC", "LTC", "DOGE", "BCH", "SOL"):
        assert _can_send(sym) is False, sym


def test_a_row_for_an_unsendable_chain_still_offers_receive():
    """Not being able to spend BTC must not stop somebody being paid in it."""
    html = _render({"known": True, "units": 1, "amount": "0.00000001", "address": "1abc"})
    assert "ex-receive" in html
