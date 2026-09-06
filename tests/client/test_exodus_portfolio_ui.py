import json
from pathlib import Path

import pytest

from app.schemas import CLIENT_THEMES
from tests.client.test_followup_regressions import browser

ROOT = Path(__file__).resolve().parents[2]


def page(tmp_path, action='', width=1280, theme='professional', mode='normal'):
    code = (ROOT / 'static/js/client/exodus.js').read_text().replace('"/static/vendor/', '"' + ROOT.as_uri() + '/static/vendor/')
    css = (ROOT / 'static/css/client.css').read_text() + (ROOT / 'static/css/exodus.css').read_text()
    css += 'body{display:block!important;margin:0;padding:12px}#feed{width:100%;max-width:1100px;margin:auto;overflow:visible}'
    setup = f'''
document.documentElement.dataset.theme={json.dumps(theme)};
const SECOND='1'.repeat(32), mode={json.dumps(mode)};let releaseBalance,releaseAddress,releaseConfirm,calls=[];
let balancePoll;
if(mode==='poll'){{const timer=window.setTimeout;window.setTimeout=(callback,delay)=>{{if(delay===15000||delay===60000){{balancePoll=callback;return 1000000;}}return timer(callback,delay);}};}}
const wallets=[{{id:'default',name:'Main wallet'}},{{id:SECOND,name:'Savings'}}];
const reply=data=>({{ok:true,status:200,json:async()=>data}});
const dataFor=wallet=>({{balances:{{BTC:{{known:true,amount:'1'}},ETH:{{known:true,amount:'2'}}}},valuation:{{complete:true,total:wallet==='default'?'900.00':'200.00',known_total:wallet==='default'?'900.00':'200.00',missing:[],assets:{{BTC:{{usd:wallet==='default'?'700.00':'100.00'}},ETH:{{usd:wallet==='default'?'200.00':'100.00'}}}},prices_at:Math.floor(Date.now()/1000)}},history:{{available:true,points:[{{at:Math.floor(Date.now()/1000)-3600,usd:'100'}},{{at:Math.floor(Date.now()/1000),usd:'200'}}]}}}});
window.__PC_API_BASE__='https://instance.example';
window.__PC={{$:s=>document.querySelector(s),enc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),viewer:()=>({{pubkey:'account'}}),isView:()=>true,toast:()=>{{}},ensureAiSession:async()=>{{}},
uiConfirm:()=>mode==='confirm'?new Promise(r=>releaseConfirm=r):Promise.resolve(true),
authFetch:async(path,opts)=>{{const url=new URL(path,'https://instance.example'),wallet=url.searchParams.get('wallet')||'default';calls.push(url.pathname+'?'+url.searchParams);
if(url.pathname.endsWith('/wallets'))return reply({{wallets}});
if(url.pathname.endsWith('/status'))return reply({{exists:true,label:wallet==='default'?'Main wallet':'Savings',backedUp:true,portfolios:[{{id:0,name:'Main'}},{{id:1,name:'Long term'}}],chains:[{{symbol:'BTC',name:'Bitcoin'}},{{symbol:'ETH',name:'Ethereum'}}]}});
if(url.pathname.endsWith('/balances')){{if(mode==='balance'&&wallet==='default')return new Promise(r=>releaseBalance=()=>r(reply(dataFor(wallet))));return reply(dataFor(wallet));}}
if(url.pathname.endsWith('/addresses')){{if(mode==='address')return new Promise(r=>releaseAddress=()=>r(reply({{addresses:{{BTC:'OLD-WALLET-ADDRESS'}}}})));return reply({{addresses:{{BTC:'address-'+wallet,ETH:'0x123'}}}});}}
if(url.pathname.endsWith('/send-status'))return reply({{state:'idle'}});
if(url.pathname.endsWith('/reveal'))return reply({{mnemonic:'SHOULD-NOT-APPEAR'}});
return reply({{}});}}}};
'''
    return browser(tmp_path, '<main id="feed"></main>', setup + code + f'''
PCExodus.render();
setTimeout(async()=>{{
{action}
}},180);
''', width, css)


@pytest.mark.parametrize('theme', CLIENT_THEMES)
@pytest.mark.parametrize('width', [390, 1280])
def test_portfolio_dashboard_has_working_logos_charts_and_no_overflow(tmp_path, theme, width):
    result = page(tmp_path, '''
const svg=document.querySelector('.ex-value-chart'),images=[...document.querySelectorAll('.ex-asset-logo')];
document.querySelector('#result').textContent=JSON.stringify({total:document.querySelector('.ex-total').textContent,
fits:document.documentElement.scrollWidth<=innerWidth+1,logos:images.length===2&&images.every(i=>i.complete&&i.naturalWidth>0),
chart:!!svg&&!svg.innerHTML.includes('NaN'),wallets:document.querySelector('#ex-wallet').options.length});
''', width, theme)
    assert result == {'total': '$900.00', 'fits': True, 'logos': True, 'chart': True, 'wallets': 2}, result


def test_late_balance_from_previous_wallet_cannot_replace_current_total(tmp_path):
    result = page(tmp_path, '''
const select=document.querySelector('#ex-wallet');select.value=SECOND;select.onchange();
setTimeout(()=>{releaseBalance();setTimeout(()=>{document.querySelector('#result').textContent=JSON.stringify({
wallet:document.querySelector('#ex-wallet').value,total:document.querySelector('.ex-total').textContent});},50);},50);
''', mode='balance')
    assert result == {'wallet': '1'*32, 'total': '$200.00'}


def test_late_receive_address_does_not_appear_in_the_next_wallet(tmp_path):
    result = page(tmp_path, '''
document.querySelector('.ex-receive').click();
setTimeout(()=>{const select=document.querySelector('#ex-wallet');select.value=SECOND;select.onchange();
setTimeout(()=>{releaseAddress();setTimeout(()=>{document.querySelector('#result').textContent=JSON.stringify({
wallet:document.querySelector('#ex-wallet').value,leaked:document.querySelector('#feed').textContent.includes('OLD-WALLET-ADDRESS')});},50);},50);},20);
''', mode='address')
    assert result == {'wallet': '1'*32, 'leaked': False}


def test_recovery_confirmation_for_previous_wallet_cannot_reveal_current_wallet(tmp_path):
    result = page(tmp_path, '''
document.querySelector('#ex-reveal').click();const select=document.querySelector('#ex-wallet');select.value=SECOND;select.onchange();
setTimeout(()=>{releaseConfirm(true);setTimeout(()=>{document.querySelector('#result').textContent=JSON.stringify({
revealed:calls.some(p=>p.startsWith('/api/wallet/exodus/reveal')),leaked:document.querySelector('#feed').textContent.includes('SHOULD-NOT-APPEAR')});},50);},50);
''', mode='confirm')
    assert result == {'revealed': False, 'leaked': False}


def test_incomplete_total_and_empty_history_do_not_invent_zero_or_a_graph(tmp_path):
    result = page(tmp_path, '''
document.querySelector('#feed').innerHTML=PCExodus._dashboard({valuation:{complete:false,total:null,known_total:'0.00',missing:['BTC'],assets:{}},history:{available:true,points:[]}});
document.querySelector('#result').textContent=JSON.stringify({partial:document.querySelector('#feed').textContent.includes('incomplete total'),graph:!!document.querySelector('.ex-value-chart'),nan:document.querySelector('#feed').innerHTML.includes('NaN')});
''')
    assert result == {'partial': True, 'graph': False, 'nan': False}


@pytest.mark.parametrize('confirm', [False, True])
def test_double_clicking_send_opens_one_confirmation_and_submits_at_most_once(tmp_path, confirm):
    action = """
document.querySelector('.ex-send').click();
setTimeout(()=>{
document.querySelector('#ex-to').value='0x'+'11'.repeat(20);document.querySelector('#ex-amt').value='0.1';
const go=document.querySelector('#ex-send-go');go.onclick();go.onclick();releaseConfirm(CONFIRM);
setTimeout(()=>{document.querySelector('#result').textContent=JSON.stringify({sends:calls.filter(p=>p.startsWith('/api/wallet/exodus/send?')).length,disabled:go.disabled});},60);
},30);
""".replace('CONFIRM', json.dumps(confirm))
    result = page(tmp_path, action, mode='confirm')
    assert result['sends'] == int(confirm)
    assert result['disabled'] == confirm


def test_downloaded_recovery_backup_keeps_separate_monero_words_and_hides_them(tmp_path):
    result = page(tmp_path, '''
let saved=null;
const fetchBefore=__PC.authFetch;
__PC.authFetch=async(path,opts)=>path.includes('/reveal?')?reply({mnemonic:'public-bip39-fixture',moneroMnemonic:'public-monero-fixture',derivation:'exodus-v1'}):fetchBefore(path,opts);
__PC.saveBlobAs=async(blob,name)=>{saved={name,data:JSON.parse(await blob.text())};};
const before=!!document.querySelector('#ex-backup-download');
document.querySelector('#ex-reveal').click();
setTimeout(async()=>{
await document.querySelector('#ex-backup-download').onclick();
document.querySelector('#ex-panel-close').click();
document.querySelector('#result').textContent=JSON.stringify({before,saved,hidden:!document.querySelector('#feed').textContent.includes('public-monero-fixture')});
},40);
''')
    assert result == {'before':False,'saved':{'name':'wallet-recovery.json','data':{
        'format':'cloudos-wallet-backup-v1','mnemonic':'public-bip39-fixture',
        'moneroMnemonic':'public-monero-fixture','derivation':'exodus-v1'}},'hidden':True}


def test_add_wallet_sends_both_recovery_phrases_only_after_submit(tmp_path):
    result = page(tmp_path, '''
let imported=null;
const fetchBefore=__PC.authFetch;
__PC.authFetch=async(path,opts)=>{
 if(path.endsWith('/wallets')&&opts?.method==='POST'){imported=JSON.parse(opts.body);return reply({id:SECOND});}
 return fetchBefore(path,opts);
};
document.querySelector('#ex-add-wallet').click();
document.querySelector('#ex-new-name').value='Imported';
document.querySelector('#ex-new-phrase').value='public-bip39-fixture';
document.querySelector('#ex-new-monero').value='public-monero-fixture';
const before=imported;
document.querySelector('#ex-add-form').dispatchEvent(new Event('submit',{cancelable:true}));
setTimeout(()=>{document.querySelector('#result').textContent=JSON.stringify({before,imported,wallet:document.querySelector('#ex-wallet').value});},50);
''')
    assert result == {'before':None,'imported':{'label':'Imported','mnemonic':'public-bip39-fixture','moneroMnemonic':'public-monero-fixture'},'wallet':'1'*32}


def test_legacy_recovery_format_is_explicitly_sent_when_restoring_a_wallet(tmp_path):
    result = page(tmp_path, '''
let imported=null;
const fetchBefore=__PC.authFetch;
__PC.authFetch=async(path,opts)=>{
 if(path.endsWith('/wallets')&&opts?.method==='POST'){imported=JSON.parse(opts.body);return reply({id:SECOND});}
 return fetchBefore(path,opts);
};
document.querySelector('#ex-add-wallet').click();
document.querySelector('#ex-new-name').value='Legacy wallet';
document.querySelector('#ex-new-phrase').value='public-legacy-fixture';
document.querySelector('#ex-new-format').value='cloudos-v1';
document.querySelector('#ex-add-form').dispatchEvent(new Event('submit',{cancelable:true}));
setTimeout(()=>{document.querySelector('#result').textContent=JSON.stringify(imported);},50);
''')
    assert result == {'label':'Legacy wallet','mnemonic':'public-legacy-fixture','derivation':'cloudos-v1'}


def test_balance_poll_preserves_open_send_form_and_then_updates_the_total(tmp_path):
    result = page(tmp_path, '''
let balanceReads=0;
const fetchBefore=__PC.authFetch;
__PC.authFetch=async(path,opts)=>{
 if(path.includes('/balances?')){balanceReads++;const next=dataFor('default');next.valuation.total='1234.00';return reply(next);}
 return fetchBefore(path,opts);
};
document.querySelector('.ex-send').click();
setTimeout(()=>{
 const input=document.querySelector('#ex-to');input.value='keep-this-recipient';input.focus();
 const before=balanceReads;
 balancePoll();
 const retained=input===document.activeElement&&input.value==='keep-this-recipient';
 const idle=balanceReads===before;
 document.querySelector('#ex-panel-close').click();balancePoll();
 setTimeout(()=>{document.querySelector('#result').textContent=JSON.stringify({retained,idle,total:document.querySelector('.ex-total').textContent});},50);
},30);
''', mode='poll')
    assert result == {'retained':True,'idle':True,'total':'$1,234.00'}


def test_balance_poll_stops_after_leaving_the_wallet(tmp_path):
    result = page(tmp_path, '''
const before=calls.length;
__PC.isView=()=>false;
balancePoll();
setTimeout(()=>{document.querySelector('#result').textContent=JSON.stringify({noRequest:calls.length===before});},30);
''', mode='poll')
    assert result == {'noRequest':True}


@pytest.mark.parametrize('container_width', [320, 390, 560, 900])
def test_asset_cards_keep_controls_reachable_inside_desktop_windows(tmp_path, container_width):
    result = page(tmp_path, '''
const feed=document.querySelector('#feed');feed.style.width=''' + str(container_width) + '''+'px';
setTimeout(()=>{
  const cards=[...document.querySelectorAll('.ex-coin')];
  const controls=cards.flatMap(card=>[...card.querySelectorAll('button')].map(button=>{
    const a=card.getBoundingClientRect(),b=button.getBoundingClientRect();
    button.scrollIntoView({block:'center'});const r=button.getBoundingClientRect();
    const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    return b.width>35&&b.height>25&&b.left>=a.left&&b.right<=a.right+1&&(hit===button||button.contains(hit));
  }));
  const a=cards[0].getBoundingClientRect(),b=cards[1].getBoundingClientRect();
  document.querySelector('.ex-receive').click();
  setTimeout(()=>{document.querySelector('#result').textContent=JSON.stringify({
    controls:controls.every(Boolean),overlap:!(a.right<=b.left||b.right<=a.left||a.bottom<=b.top||b.bottom<=a.top),
    fits:feed.scrollWidth<=feed.clientWidth+1,receive:document.querySelector('#ex-panel').textContent.includes('address-default')});},50);
},50);
''')
    assert result == {'controls': True, 'overlap': False, 'fits': True, 'receive': True}, result


@pytest.mark.parametrize('theme', CLIENT_THEMES)
def test_allocation_categories_have_distinct_visible_colors(tmp_path, theme):
    result = page(tmp_path, '''
const segments=[...document.querySelectorAll('.ex-ring-part')];
document.querySelector('#result').textContent=JSON.stringify({
  distinct:new Set(segments.map(node=>getComputedStyle(node).stroke)).size===segments.length,
  labels:document.querySelector('.ex-allocation-list').textContent.includes('77.8%')});
''', theme=theme)
    assert result == {'distinct': True, 'labels': True}, result
