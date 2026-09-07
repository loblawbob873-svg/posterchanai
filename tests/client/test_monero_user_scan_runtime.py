"""Drive actual per-user QR/paste handlers with fake native/camera APIs, never payments."""
import json
import subprocess

import pytest

from tests.client.test_monero_user_send_runtime import BOOT, SETUP

SCAN_SETUP = r'''
async function openSheet(){
 await PCMoneroWallet.render();nodes.get('mw-me-send').onclick();
 const sheet=modals.at(-1);assert.match(sheet.html,/mw-ms-scan/);
 assert.equal(typeof sheet.querySelector('#mw-ms-scan').onclick,'function');
 return sheet;
}
const address='4'+'A'.repeat(94);
const payment='monero:'+address+'?tx_amount=0.123456789012&recipient_name=Alice';
'''


def run(script):
    result = subprocess.run(['node','-e',BOOT+SETUP+SCAN_SETUP+'\n(async()=>{'+script+'\ndone({ok:true});})().catch(e=>{console.error(e);process.exitCode=1;});'],
                            capture_output=True,text=True,timeout=30)
    assert result.returncode == 0,result.stderr[-4000:]
    assert json.loads(result.stdout)['ok']


def test_native_qr_populates_mainnet_address_and_exact_amount_without_sending():
    run('''let scans=0;globalThis.Capacitor={Plugins:{QrScan:{scan:async()=>{scans++;return {text:payment};}}}};
    const sheet=await openSheet();await sheet.querySelector('#mw-ms-scan').onclick();
    assert.equal(scans,1);assert.equal(sheet.querySelector('#mw-ms-to').value,address);
    assert.equal(sheet.querySelector('#mw-ms-amount').value,'0.123456789012');
    assert.equal(posts.length,0);assert.equal(modals.at(-1),sheet);''')


def test_native_qr_for_wrong_network_leaves_existing_fields_untouched():
    run('''globalThis.Capacitor={Plugins:{QrScan:{scan:async()=>({text:'monero:5'+'A'.repeat(94)+'?tx_amount=0.5'})}}};
    const sheet=await openSheet();sheet.querySelector('#mw-ms-to').value='keep';sheet.querySelector('#mw-ms-amount').value='0.2';
    await sheet.querySelector('#mw-ms-scan').onclick();
    assert.equal(sheet.querySelector('#mw-ms-to').value,'keep');assert.equal(sheet.querySelector('#mw-ms-amount').value,'0.2');
    assert(toasts.some(t=>/network/i.test(t)));assert.equal(posts.length,0);''')


@pytest.mark.parametrize('failure',[False,True])
def test_native_cancel_or_failure_never_sends_or_opens_confirmation(failure):
    run('''globalThis.Capacitor={Plugins:{QrScan:{scan:async()=>{%s}}}};
    const sheet=await openSheet();await sheet.querySelector('#mw-ms-scan').onclick();
    assert.equal(posts.length,0);assert.equal(modals.at(-1),sheet);
    assert.equal(sheet.querySelector('#mw-ms-to').value,'');''' % ("throw Error('cancelled');" if failure else "return {text:''};"))


def test_unsupported_camera_focuses_per_user_address_for_paste():
    run('''const sheet=await openSheet();let focused=false;sheet.querySelector('#mw-ms-to').focus=()=>focused=true;
    await sheet.querySelector('#mw-ms-scan').onclick();assert(focused);
    assert(toasts.some(t=>/paste.*Monero URI/i.test(t)));assert.equal(posts.length,0);''')


def test_pasted_monero_uri_reviews_address_and_amount_without_sending():
    run('''const sheet=await openSheet();sheet.querySelector('#mw-ms-to').value=payment;
    sheet.querySelector('#mw-ms-review').onclick();
    const confirm=modals.at(-1);assert.notEqual(confirm,sheet);
    assert(confirm.html.includes(address));assert(confirm.html.includes('0.123456789012'));
    assert.equal(posts.length,0);''')


def test_camera_permission_denied_never_sends():
    run('''globalThis.BarcodeDetector=class{static async getSupportedFormats(){return ['qr_code'];}};
    navigator.mediaDevices={getUserMedia:async()=>{throw Error('denied');}};
    const sheet=await openSheet();await sheet.querySelector('#mw-ms-scan').onclick();
    assert.equal(posts.length,0);assert.equal(modals.at(-1),sheet);
    assert(toasts.some(t=>/camera unavailable/i.test(t)));''')


def test_camera_cancel_stops_tracks_and_never_sends():
    run('''globalThis.BarcodeDetector=class{static async getSupportedFormats(){return ['qr_code'];}async detect(){return [];}};
    let stopped=0;navigator.mediaDevices={getUserMedia:async()=>({getTracks:()=>[{stop:()=>stopped++}]})};
    const sheet=await openSheet(),video={play:async()=>{}};
    sheet.querySelector('#mw-scan-stage').querySelector=()=>video;
    await sheet.querySelector('#mw-ms-scan').onclick();sheet.querySelector('#mw-scan-cancel').onclick();
    await new Promise(r=>setTimeout(r,300));assert(stopped>=1);assert.equal(posts.length,0);''')


def test_duplicate_native_scan_click_uses_one_session():
    run('''let calls=0,release;globalThis.Capacitor={Plugins:{QrScan:{scan:()=>{calls++;return new Promise(r=>release=r);}}}};
    const sheet=await openSheet(),scan=sheet.querySelector('#mw-ms-scan');
    const first=scan.onclick();await scan.onclick();assert.equal(calls,1);
    release({text:payment});await first;assert.equal(posts.length,0);
    assert.equal(sheet.querySelector('#mw-ms-to').value,address);''')


def test_cancel_pending_camera_stops_late_stream_without_playing():
    run('''globalThis.BarcodeDetector=class{static async getSupportedFormats(){return ['qr_code'];}async detect(){return [];}};
    let release,stops=0,plays=0;navigator.mediaDevices={getUserMedia:()=>new Promise(r=>release=r)};
    const sheet=await openSheet();sheet.querySelector('#mw-scan-stage').querySelector=()=>({play:async()=>plays++});
    const pending=sheet.querySelector('#mw-ms-scan').onclick();await tick();
    sheet.querySelector('#mw-scan-cancel').onclick();release({getTracks:()=>[{stop:()=>stops++}]});await pending;
    assert.equal(plays,0);assert.equal(stops,1);assert.equal(posts.length,0);''')


def test_old_camera_completion_cannot_evict_replacement_scan_guard():
    run('''globalThis.BarcodeDetector=class{static async getSupportedFormats(){return ['qr_code'];}async detect(){return [];}};
    const permissions=[];let calls=0,stops=0,plays=0;
    navigator.mediaDevices={getUserMedia:()=>{calls++;return new Promise(r=>permissions.push(r));}};
    const sheet=await openSheet(),scan=sheet.querySelector('#mw-ms-scan'),stage=sheet.querySelector('#mw-scan-stage');
    stage.querySelector=()=>({play:async()=>plays++});
    const old=scan.onclick();await tick();sheet.querySelector('#mw-scan-cancel').onclick();
    const replacement=scan.onclick();await tick();assert.equal(calls,2);
    permissions[0]({getTracks:()=>[{stop:()=>stops++}]});await old;
    await scan.onclick();assert.equal(calls,2,'stale cleanup removed replacement session guard');
    sheet.querySelector('#mw-scan-cancel').onclick();permissions[1]({getTracks:()=>[{stop:()=>stops++}]});await replacement;
    assert.equal(plays,0);assert.equal(stops,2);assert.equal(posts.length,0);''')
