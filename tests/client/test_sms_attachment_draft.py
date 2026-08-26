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


def test_pending_send_has_a_touch_accessible_cancel_action():
    assert 'class="sms-cancel-pending"' in JS
    assert "m.pending?'Cancel send':'Delete'" in JS
    assert '!m.incoming' in JS
    assert 'aria-label="${m.pending?' in JS
    assert "feed.querySelectorAll('.sms-cancel-pending')" in JS
    assert "await remove([cancel.dataset.doc])" in JS
    assert ".sms-cancel-pending{" in CSS
