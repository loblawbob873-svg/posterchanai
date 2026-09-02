"""The micro-wallet must stay a keyless, stagenet-labelled optional enhancement."""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS = os.path.join(ROOT, "static", "js", "client", "monero-wallet.js")
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
TPL = os.path.join(ROOT, "templates", "client.html")
ADDR = "5" + "A" * 94

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def node(expr):
    script = "const w=require(%s);process.stdout.write(JSON.stringify(%s));" % (json.dumps(JS), expr)
    done = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_monero_uri_encodes_amount_and_recipient():
    assert node("w.uri(%s,'0.001','Alice & Bob')" % json.dumps(ADDR)) == (
        "monero:" + ADDR + "?tx_amount=0.001&recipient_name=Alice%20%26%20Bob")


def test_payment_qr_uri_round_trips_without_float_or_html_interpretation():
    got = node("w.parsePaymentUri(%s)" % json.dumps(
        "monero:" + ADDR + "?tx_amount=123456789.123456789012&recipient_name=Alice%20%26%20Bob"))
    assert got == {"address": ADDR, "amount": "123456789.123456789012", "recipient": "Alice & Bob"}


def test_payment_qr_parser_rejects_mainnet_duplicates_precision_and_controls():
    expr = "[" + ",".join("w.parsePaymentUri(%s)" % json.dumps(v) for v in (
        "monero:" + "4" + "A" * 94,
        "monero:" + ADDR + "?tx_amount=1&tx_amount=2",
        "monero:" + ADDR + "?tx_amount=0.0000000000001",
        "monero:" + ADDR + "?recipient_name=bad%0Aname",
        "https://example.test/" + ADDR,
    )) + "]"
    assert node(expr) == [None] * 5


def test_bare_stagenet_address_qr_is_accepted_and_zero_amount_is_left_blank():
    assert node("w.parsePaymentUri(%s)" % json.dumps(ADDR)) == {
        "address": ADDR, "amount": "", "recipient": ""}
    assert node("w.parsePaymentUri(%s).amount" % json.dumps("monero:" + ADDR + "?tx_amount=0.000")) == ""


def test_address_validation_is_stagenet_only_and_rejects_injection():
    assert node("[w.validAddress(%s),w.validAddress('monero:'+%s),w.validAddress(%s+';x')]" %
                (json.dumps(ADDR), json.dumps(ADDR), json.dumps(ADDR))) == [True, False, False]
    assert node("w.validAddress('4'+'A'.repeat(94))") is False


def test_address_validation_can_explicitly_select_mainnet():
    assert node("w.validAddress('4'+'A'.repeat(94),'mainnet')") is True
    assert node("w.validAddress('8'+'A'.repeat(94),'mainnet')") is True
    assert node("w.validAddress('5'+'A'.repeat(94),'mainnet')") is False


def test_atomic_amounts_are_exact_even_below_one_micro_xmr():
    assert node("[w.format(500000,true),w.format(1230000000000,true)]") == ["0.0000005", "1.23"]


def test_large_atomic_balance_never_crosses_number_precision_boundary():
    # Beyond Number.MAX_SAFE_INTEGER, including all twelve fractional digits.
    assert node("w.format('1234567890123456789012345',true)") == "1,234,567,890,123.456789012345"


def test_positive_outgoing_rpc_amount_is_sent_and_epoch_is_readable():
    row = node("w.transferView({direction:'out',amount_atomic:'9007199254740993123',timestamp:1700000000})")
    assert row["incoming"] is False
    assert row["amount"] == "9,007,199.254740993123"
    assert row["date"] != "1700000000"
    assert row["date"] != "pending"


def test_incoming_bucket_is_received_even_when_amount_string_has_no_sign():
    row = node("w.transferView({direction:'in',amount_atomic:'1',timestamp:1700000000000})")
    assert row["incoming"] is True
    assert row["amount"] == "0.000000000001"


def test_wallet_is_wired_as_optional_module_and_tip_falls_back():
    app = open(APP, encoding="utf-8").read()
    tpl = open(TPL, encoding="utf-8").read()
    assert "renderModuleView('wallet','monero-wallet.js','PCMoneroWallet','render')" in app
    # The module is LOADED on demand and then asked. Testing the global alone made the built-in
    # wallet depend on whether the Wallet screen had been opened this session — "monero android app
    # not using built-in wallet! desktop works but not android".
    assert "_withModule('monero-wallet.js', 'PCMoneroWallet')" in app
    assert "await _xmrWallet.tip" in app
    assert "catch(_){}" in app[app.index("await _xmrWallet.tip"):][:500]
    assert 'data-view="wallet"' in tpl
    assert '/static/js/client/monero-wallet.js' in tpl


def test_wallet_click_owns_boot_landing_and_more_uses_real_coin_sprite():
    app = open(APP, encoding="utf-8").read()
    activate = app[app.index("function activateNavView"):app.index("function timelineTop")]
    assert "requestView(target);" in activate
    assert "['wallet','coin','Monero Wallet']" in app
    assert "['wallet','wallet','Monero Wallet']" not in app


def test_late_wallet_requests_respect_shared_feed_ownership_at_runtime():
    script = r'''
    let writes=0, html='';
    const feed={classList:{},get innerHTML(){return html},set innerHTML(v){writes++;html=v},querySelectorAll(){return[]}};
    global.document={getElementById:id=>id==='feed'?feed:null};
    global.__PC={VIEW:'wallet',toast(){},modal(){},closeModal(){}};
    global.fetch=async url=>{await new Promise(r=>setTimeout(r,20));
      const body=url.includes('balance')?{balance:'1',unlocked_balance:'1'}:url.includes('address')?{address:'5'+'A'.repeat(94)}:{in:[],out:[]};
      return new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}})};
    require(process.argv[1]);
    setTimeout(async()=>{
      const first=global.PCMoneroWallet.render(true); global.__PC.VIEW='social'; await first;
      const afterLeave=writes; global.__PC.VIEW='wallet';
      await Promise.all([global.PCMoneroWallet.render(true),global.PCMoneroWallet.render(true)]);
      const afterResume=writes; await global.PCMoneroWallet.render(false);
      process.stdout.write(JSON.stringify({afterLeave,afterResume,final:writes,hasWallet:html.includes('Monero Wallet')}));
    },10);
    '''
    done = subprocess.run(["node", "-e", script, JS], text=True, capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["afterLeave"] == 0
    assert got["afterResume"] >= 1 and got["final"] >= got["afterResume"]
    assert got["hasWallet"] is True


def test_keys_never_enter_the_client_module():
    src = open(JS, encoding="utf-8").read().lower()
    for forbidden in ("spend_key", "private_view", "mnemonic", "seed phrase", "nsec"):
        assert forbidden not in src
    assert "stagenet" in src
    assert "/api/wallet/xmr/transfer/prepare" in src
    assert "/api/wallet/xmr/transfer/confirm" in src
    assert "cannot be reversed" in src
    assert "open external wallet instead" in src.lower()
    assert "scan wallet qr" in src.lower()
    assert "capacitor.plugins.qrscan" in src
    assert "barcodedetector" in src
