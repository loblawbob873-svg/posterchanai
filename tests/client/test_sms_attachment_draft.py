from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static/js/client/sms.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_selected_mms_is_visible_and_removable_before_send():
    assert 'class="sms-attachment-draft"' in JS
    assert 'ready to send as MMS' in JS
    assert 'id="sms-attach-clear"' in JS
    assert "clear.onclick=()=>{clearAttachment();paint();}" in JS
    assert ".sms-attachment-draft{" in CSS


def test_attachment_cannot_leak_to_another_recipient():
    assert "b.onclick = () => { clearAttachment(); S.open = b.dataset.k; paint(); };" in JS
    assert "PC.$('#sms-back').onclick = () => { clearAttachment(); S.open = ''; paint(); };" in JS
    assert "clearAttachment(); S.open = key(to);" in JS


def test_mms_send_captures_the_displayed_file_and_rejects_non_images():
    assert "if(file&&!isImageFile(file))return {ok:false,error:'MMS currently supports photos'};" in JS
    assert "const attachment=S.attach;" in JS
    assert "send(t.address, body, attachment)" in JS
    assert "if(S.attach===attachment)clearAttachment();" in JS


def test_texts_attachment_menu_offers_camera_device_and_blossom():
    assert 'id="sms-camera" type="file" accept="image/*" capture="environment"' in JS
    assert 'id="sms-src-camera"' in JS
    assert 'id="sms-src-device"' in JS
    assert 'id="sms-src-blossom"' in JS
    assert "PC.blossomPicker(null, async ({url,type,ext})" in JS
    assert "filter:b=>String(b.type||'').startsWith('image/')" in JS
    assert "acceptFile(new File([blob],name" in JS


def test_native_blossom_launch_opens_picker_in_the_original_conversation():
    phone = (ROOT / 'static/js/client/phoneshell.js').read_text()
    assert "v.indexOf('texts-blossom:')===0" in phone
    assert "PCSms.openBlossom(address)" in phone
    assert "openBlossom: address =>" in JS
    assert "blossomLaunch=true;paint()" in JS
    assert "if(blossomLaunch){blossomLaunch=false;setTimeout(fromBlossom,0);}" in JS


def test_texts_media_opens_in_the_shared_fullscreen_viewer():
    assert "PC.openLightbox(url, 'image')" in JS
    assert "PC.openLightbox(d.url, 'video')" in JS
    assert 'className = \'sms-att-open\'' in JS
    assert "v.onclick" not in JS, "native video controls must not be hijacked by the viewer action"
    lightbox = (ROOT / "static/js/client/app.js").read_text()
    css = (ROOT / "static/css/client.css").read_text()
    render = lightbox[lightbox.index("function openLightbox"):
                       lightbox.index("function _lbZoom")]
    assert "el.playsInline=true" in render
    assert ".lightbox{position:fixed;inset:0" in css
    assert ".lightbox img,.lightbox video{max-width:100%;max-height:84vh;width:auto;height:auto" in css
    assert ".sms-att-open{" in css


def test_messages_delete_from_long_press_without_an_inline_button():
    assert 'class="sms-cancel-pending"' not in JS
    assert ".sms-cancel-pending{" not in CSS
    assert "el.onpointerdown=e=>" in JS
    assert "setTimeout(()=>{hold=0;removeMessage();},550)" in JS
    assert "el.onpointermove=e=>" in JS
    assert "Math.abs(e.clientX-startX)>10" in JS
    assert "el.onpointercancel=stopHold" in JS
    assert "el.oncontextmenu = e =>" in JS
    assert "await remove([el.dataset.doc])" in JS


def test_definite_failed_sends_offer_a_guarded_retry_without_destructive_long_press():
    assert 'data-sms-retry="${enc(m.doc)}"' in JS
    retry = JS[JS.index("async function retryFailed(m)"):JS.index("function paintThread")]
    assert "!m.failed" in retry
    assert "startsWith('delivery unknown')" in retry
    assert retry.index("await send(m.address") < retry.index("await remove([m.doc])")
    assert "e.target.closest('button,a,input')" in JS
