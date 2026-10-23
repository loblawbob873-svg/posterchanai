"""EVERY WALLET BUTTON IS PRESSED AND CHECKED FOR WHAT IT DOES, not for being on the page.

The Wallet view holds money, so "the button exists" is the wrong bar: Create must ask first and send nothing
when refused, Restore must send exactly the words typed and the chosen format, the two recovery buttons must
reveal the right phrase (the 12 words vs the separate Monero words), Copy must hand the SHOWN address to the
clipboard path, and "Check transfer status" must say what the server answered and lock Send when a payment is
already out there.

Runs the shipped exodus.js through the same page as test_exodus_portfolio_ui.py: a file:// page in headless
Chrome whose only network is a stubbed `__PC.authFetch`, so no test can reach a real wallet backend. Every
phrase here is an obviously fake fixture string.
"""
import pytest

from tests.client.test_exodus_portfolio_ui import page

# Records every wallet request (path, wallet/portfolio, method, JSON body), every toast and every copy, and
# lets a test answer one endpoint itself (`routes[path]`) or patch the /status answer (`statusPatch`).
PRELUDE = '''
const sent=[],toasts=[],copies=[],routes={};let statusPatch={};
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const done=o=>{document.querySelector('#result').textContent=JSON.stringify(o);};
const fail=(status,detail)=>({ok:false,status,json:async()=>({detail})});
__PC.toast=m=>toasts.push(String(m));
__PC.copyValue=(value,ok,label)=>{copies.push([value,ok,label]);};
const fetchBase=__PC.authFetch;
__PC.authFetch=async(path,opts)=>{
 const u=new URL(path,'https://instance.example');
 sent.push({path:u.pathname,portfolio:u.searchParams.get('portfolio'),method:opts&&opts.method||'GET',
            body:opts&&opts.body?JSON.parse(opts.body):null});
 const own=routes[u.pathname];if(own){const r=await own(u,opts);if(r)return r;}
 if(u.pathname.endsWith('/status')){const s=await (await fetchBase(path,opts)).json();return reply({...s,...statusPatch});}
 return fetchBase(path,opts);
};
const posts=p=>sent.filter(s=>s.path==='/api/wallet/exodus/'+p&&s.method==='POST');
const rerender=async()=>{await PCExodus.render();await wait(30);};
'''


def run(tmp_path, action):
    return page(tmp_path, PRELUDE + action)


# ------------------------------------------------------------------ the empty wallet: create and restore

@pytest.mark.parametrize('confirmed', [True, False])
def test_create_asks_first_and_only_then_creates_and_shows_the_wallet(tmp_path, confirmed):
    result = run(tmp_path, '''
statusPatch={exists:false};
let asked=null;
__PC.uiConfirm=async(text,opts)=>{asked={text,ok:opts.ok};return CONFIRMED;};
routes['/api/wallet/exodus/create']=()=>{statusPatch={};return reply({});};
await rerender();
document.querySelector('#ex-create').click();
await wait(60);
done({asked:asked&&asked.text.startsWith('Create a wallet on this server?'),ok:asked&&asked.ok,
 creates:posts('create').map(s=>s.body),walletShown:!!document.querySelector('#ex-reveal'),
 createLeft:!!document.querySelector('#ex-create'),enabled:!!document.querySelector('#ex-create')&&!document.querySelector('#ex-create').disabled});
'''.replace('CONFIRMED', 'true' if confirmed else 'false'))
    assert result['asked'] is True and result['ok'] == 'Create'
    if confirmed:
        assert result['creates'] == [{}], 'a new wallet is created with an EMPTY body -- the server makes the phrase'
        assert result['walletShown'] and not result['createLeft']
    else:
        assert result['creates'] == [], 'refusing the confirmation still created a wallet'
        assert result['createLeft'] and result['enabled'], 'a refused Create left the button dead'


def test_create_refused_by_the_server_says_its_sentence_and_can_be_pressed_again(tmp_path):
    result = run(tmp_path, '''
statusPatch={exists:false};
routes['/api/wallet/exodus/create']=()=>fail(409,'this account already has a wallet');
await rerender();
document.querySelector('#ex-create').click();
await wait(60);
done({toasts,disabled:document.querySelector('#ex-create').disabled});
''')
    assert result == {'toasts': ['wallet not created: this account already has a wallet'], 'disabled': False}


def test_restore_button_opens_and_closes_the_phrase_box(tmp_path):
    result = run(tmp_path, '''
statusPatch={exists:false};
await rerender();
const box=document.querySelector('#ex-restore-box'),states=[box.classList.contains('hidden')];
document.querySelector('#ex-restore').click();states.push(box.classList.contains('hidden'));
document.querySelector('#ex-restore').click();states.push(box.classList.contains('hidden'));
done({states,requests:posts('create').length});
''')
    assert result == {'states': [True, False, True], 'requests': 0}


def test_restore_go_sends_exactly_the_typed_words_and_the_chosen_format(tmp_path):
    result = run(tmp_path, '''
statusPatch={exists:false};
routes['/api/wallet/exodus/create']=()=>{statusPatch={};return reply({});};
await rerender();
document.querySelector('#ex-restore').click();
// Nothing typed: refused with a sentence, nothing sent.
document.querySelector('#ex-restore-go').click();
await wait(20);
const empty={toasts:[...toasts],creates:posts('create').length};
document.querySelector('#ex-phrase').value='  fake fixture words not a seed  ';
document.querySelector('#ex-monero-phrase').value='fake monero fixture words';
document.querySelector('#ex-restore-format').value='cloudos-v1';
document.querySelector('#ex-restore-go').click();
await wait(60);
done({empty,creates:posts('create').map(s=>s.body),walletShown:!!document.querySelector('#ex-reveal')});
''')
    assert result['empty'] == {'toasts': ['paste the recovery phrase first'], 'creates': 0}
    assert result['creates'] == [{'mnemonic': 'fake fixture words not a seed',
                                  'moneroMnemonic': 'fake monero fixture words', 'derivation': 'cloudos-v1'}]
    assert result['walletShown'] is True


# ------------------------------------------------------------------ wallets and portfolios

def test_add_portfolio_creates_it_and_switches_to_it(tmp_path):
    result = run(tmp_path, '''
const added=[{id:0,name:'Main'},{id:1,name:'Long term'},{id:2,name:'Fixture portfolio'}];
routes['/api/wallet/exodus/portfolios']=()=>{statusPatch={portfolios:added};return reply({portfolios:added});};
document.querySelector('#ex-add-portfolio').click();
const form=document.querySelector('#ex-add-form');
const kind=form.querySelector('h3').textContent,hasPhrase=!!form.querySelector('#ex-new-phrase');
document.querySelector('#ex-new-name').value='Fixture portfolio';
form.querySelector('[type=submit]').click();
await wait(80);
const balances=sent.filter(s=>s.path==='/api/wallet/exodus/balances');
done({kind,hasPhrase,bodies:posts('portfolios').map(s=>s.body),selected:document.querySelector('#ex-portfolio').value,
 balancesFor:balances[balances.length-1].portfolio});
''')
    assert result == {'kind': 'Add a portfolio', 'hasPhrase': False, 'bodies': [{'name': 'Fixture portfolio'}],
                      'selected': '2', 'balancesFor': '2'}


@pytest.mark.parametrize('opener', ['#ex-add-wallet', '#ex-add-portfolio'])
def test_add_cancel_closes_the_form_and_sends_nothing(tmp_path, opener):
    result = run(tmp_path, '''
document.querySelector(OPENER).click();
document.querySelector('#ex-new-name').value='never sent';
const opened=!!document.querySelector('#ex-add-form');
document.querySelector('#ex-add-cancel').click();
await wait(30);
done({opened,panel:document.querySelector('#ex-panel').childElementCount,
 posts:sent.filter(s=>s.method==='POST').length});
'''.replace('OPENER', repr(opener)))
    assert result == {'opened': True, 'panel': 0, 'posts': 0}


# ------------------------------------------------------------------ receive, refresh, retry

def test_copy_address_hands_the_shown_address_to_copy_value(tmp_path):
    result = run(tmp_path, '''
document.querySelector('.ex-receive[data-sym="BTC"]').click();
await wait(30);
const shown=document.querySelector('.ex-addr').textContent;
document.querySelector('#ex-copy').click();
done({shown,copies});
''')
    assert result == {'shown': 'address-default',
                      'copies': [['address-default', 'address copied', 'Copy the BTC address:']]}


def test_refresh_reads_the_wallet_again_and_draws_the_new_total(tmp_path):
    result = run(tmp_path, '''
const before=document.querySelector('.ex-total').textContent;
routes['/api/wallet/exodus/balances']=u=>{const next=dataFor('default');next.valuation.total='1234.00';return reply(next);};
const from=sent.length;
document.querySelector('#ex-refresh').click();
await wait(60);
done({before,after:document.querySelector('.ex-total').textContent,read:sent.slice(from).map(s=>s.path)});
''')
    assert result['before'] == '$900.00' and result['after'] == '$1,234.00'
    assert result['read'] == ['/api/wallet/exodus/wallets', '/api/wallet/exodus/status',
                              '/api/wallet/exodus/balances']


def test_retry_after_an_unavailable_wallet_loads_it(tmp_path):
    result = run(tmp_path, '''
let down=true;
routes['/api/wallet/exodus/wallets']=()=>down?fail(503,'wallet service is restarting'):null;
await rerender();
const retry=document.querySelector('#ex-refresh');
const said=document.querySelector('.empty').textContent,label=retry.textContent;
down=false;
retry.click();
await wait(60);
done({said,label,total:document.querySelector('.ex-total')&&document.querySelector('.ex-total').textContent});
''')
    assert result == {'said': 'The wallet is unavailable: wallet service is restarting', 'label': 'Retry',
                      'total': '$900.00'}


# ------------------------------------------------------------------ recovery phrases

@pytest.mark.parametrize('confirmed', [True, False])
def test_show_it_on_the_not_backed_up_warning_reveals_the_wallet_phrase(tmp_path, confirmed):
    result = run(tmp_path, '''
statusPatch={backedUp:false};
__PC.uiConfirm=async()=>CONFIRMED;
routes['/api/wallet/exodus/reveal']=()=>reply({mnemonic:'fake fixture words not a seed',warning:'fixture warning'});
await rerender();
document.querySelector('#ex-reveal-now').click();
await wait(40);
const phrase=document.querySelector('#ex-panel .ex-phrase');
done({reveals:posts('reveal').length,monero:posts('reveal-monero').length,
 title:phrase&&document.querySelector('#ex-panel h3').textContent,phrase:phrase&&phrase.textContent});
'''.replace('CONFIRMED', 'true' if confirmed else 'false'))
    if confirmed:
        assert result == {'reveals': 1, 'monero': 0, 'title': 'Wallet recovery backup',
                          'phrase': 'fake fixture words not a seed'}
    else:
        assert result == {'reveals': 0, 'monero': 0, 'title': None, 'phrase': None}


def test_back_up_monero_reveals_the_separate_monero_words(tmp_path):
    result = run(tmp_path, '''
statusPatch={chains:[{symbol:'BTC',name:'Bitcoin'},{symbol:'XMR',name:'Monero'}]};
routes['/api/wallet/exodus/reveal-monero']=()=>reply({mnemonic:'fake monero fixture words'});
await rerender();
document.querySelector('#ex-xmr-backup').click();
await wait(40);
done({reveals:posts('reveal').length,monero:posts('reveal-monero').length,
 title:document.querySelector('#ex-panel h3').textContent,phrase:document.querySelector('#ex-panel .ex-phrase').textContent});
''')
    assert result == {'reveals': 0, 'monero': 1, 'title': 'Monero recovery phrase',
                      'phrase': 'fake monero fixture words'}


# ------------------------------------------------------------------ transfer status

@pytest.mark.parametrize('answer,text,locked', [
    ("reply({state:'accepted',hash:'fixture-hash'})", 'Sent. Transaction fixture-hash', True),
    ("reply({state:'submitted',hash:'fixture-hash'})", 'Transfer remains unconfirmed. Transaction fixture-hash', True),
    ("reply({state:'not_sent'})", 'The previous attempt did not reach the network.', False),
    ("fail(503,'down')", 'Transfer status could not be confirmed. Check again before sending.', True),
])
def test_check_transfer_status_says_what_the_server_answered(tmp_path, answer, text, locked):
    result = run(tmp_path, '''
document.querySelector('.ex-send[data-sym="BTC"]').click();
await wait(40);
const go=document.querySelector('#ex-send-go'),openedEnabled=!go.disabled;
routes['/api/wallet/exodus/send-status']=()=>ANSWER;
const from=sent.length;
document.querySelector('#ex-send-status').click();
await wait(40);
done({openedEnabled,asked:sent.slice(from).filter(s=>s.path==='/api/wallet/exodus/send-status').map(s=>s.body),
 text:document.querySelector('#ex-send-out').textContent,locked:go.disabled});
'''.replace('ANSWER', answer))
    assert result == {'openedEnabled': True, 'asked': [{'symbol': 'BTC'}], 'text': text, 'locked': locked}
