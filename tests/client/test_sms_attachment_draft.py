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


def test_mms_send_captures_the_displayed_file_and_accepts_photos_or_videos_only():
    assert "if(file&&!isMmsFile(file))return {ok:false,error:'MMS supports photos and videos'};" in JS
    assert 'accept="image/*,video/*"' in JS
    assert "const attachment=S.attach;" in JS
    assert "send(t.address, body, attachment)" in JS
    assert "if(S.attach===attachment)clearAttachment();" in JS


def test_video_uses_the_same_mms_route_on_phone_and_remote_clients():
    send = JS[JS.index("async function send(to, body, file)"):
              JS.index("async function outboxId", JS.index("async function send(to, body, file)"))]
    assert "MMS currently supports photos" not in send
    assert "mmsMime(file)" in send
    assert "attachment = {sha, mime" in send
    assert "mime, name:file.name" in send


def test_extension_only_video_is_not_mislabeled_as_a_photo():
    mime = JS[JS.index("function mmsMime(file)"):
              JS.index("const now =", JS.index("function mmsMime(file)"))]
    assert "mp4:'video/mp4'" in mime
    assert "mov:'video/quicktime'" in mime
    assert "webm:'video/webm'" in mime
    assert "'3gp':'video/3gpp'" in mime


def test_texts_attachment_menu_offers_camera_device_and_readable_files():
    assert 'id="sms-camera" type="file" accept="image/*" capture="environment"' in JS
    assert 'id="sms-src-camera"' in JS
    assert 'id="sms-src-device"' in JS
    assert 'id="sms-src-blossom"' in JS
    assert "PC.blossomPicker(null, async ({url,type,ext,name})" in JS
    assert "filter:b=>/^(?:image|video)\\//" in JS
    assert "acceptFile(new File([blob],pickedName" in JS
    assert "title:'📁 Attach photo or video from Files'" in JS


def test_attach_files_uses_connected_instances_cors_safe_media_reader():
    app = (ROOT / "static/js/client/app.js").read_text()
    picker = JS[JS.index("const fromBlossom ="):
                JS.index("if(blossomLaunch)", JS.index("const fromBlossom ="))]
    assert "PC.fetchMediaBlob(url)" in picker
    assert "blob=(await PC.fetchMediaBlob(url)).blob" in picker
    assert picker.index("PC.fetchMediaBlob(url)") < picker.index("else { const res=await fetch(url)")
    assert "saveBlobAs, fetchMediaBlob" in app
    fetcher = app[app.index("async function fetchMediaBlob(src)"):
                  app.index("async function sniffExt", app.index("async function fetchMediaBlob(src)"))]
    assert "credentials:'include'" in fetcher
    assert "credentials:'omit'" in fetcher
    assert "/client/proxy-image?url=" in fetcher


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
    assert ".lightbox{position:fixed;inset:0;z-index:100000" in css
    assert "document.documentElement.appendChild(bg)" in render
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
    assert "ambiguousMmsError(m.error)" in retry
    assert retry.index("await send(m.address") < retry.index("await remove([m.doc])")
    assert "e.target.closest('button,a,input')" in JS
