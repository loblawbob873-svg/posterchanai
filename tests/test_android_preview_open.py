from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PLUGIN=(ROOT/'mobile/android/app/src/main/java/place/poster/app/preview/OpenFilePlugin.java').read_text()
MAIN=(ROOT/'mobile/android/app/src/main/java/place/poster/app/MainActivity.java').read_text()
MANIFEST=(ROOT/'mobile/android/app/src/main/AndroidManifest.xml').read_text()
PREVIEW=(ROOT/'static/js/client/preview.js').read_text()


def test_pdf_fallback_reaches_a_real_android_view_intent():
    assert 'registerPlugin(place.poster.app.preview.OpenFilePlugin.class)' in MAIN
    assert 'new Intent(Intent.ACTION_VIEW)' in PLUGIN
    assert 'FLAG_GRANT_READ_URI_PERMISSION' in PLUGIN
    assert 'FileProvider.getUriForFile' in PLUGIN
    assert 'resolveActivity' in PLUGIN
    assert '.setClipData(' not in PLUGIN.split('view.setClipData', 1)[0].split('Intent view=', 1)[-1]
    assert 'view.setClipData(' in PLUGIN
    assert 'view.addFlags(' in PLUGIN


def test_android_package_visibility_exposes_installed_pdf_viewers_to_the_fallback():
    queries=MANIFEST[MANIFEST.index('<queries>'):MANIFEST.index('</queries>')]
    pdf=queries[queries.index('android:mimeType="application/pdf"')-180:]
    assert 'android:name="android.intent.action.VIEW"' in pdf[:180]
    assert 'android:name="android.intent.category.DEFAULT"' in pdf[:240]


def test_native_open_is_private_bounded_and_cleans_previous_preview():
    assert 'getCacheDir(),"preview-open"' in PLUGIN
    assert 'MAX = 32 * 1024 * 1024' in PLUGIN
    assert 'for(File f:old)if(f!=null)f.delete()' in PLUGIN
    assert 'getExternal' not in PLUGIN


def test_oversized_native_preview_is_rejected_before_base64_allocation():
    assert 'MAX_ENCODED = ((MAX + 2) / 3) * 4' in PLUGIN
    guard=PLUGIN.index('if(encoded.length()>MAX_ENCODED)')
    decode=PLUGIN.index('Base64.decode(encoded,Base64.DEFAULT)')
    assert guard < decode


def test_pdf_viewer_failure_retains_save_or_share_fallback():
    block=PREVIEW[PREVIEW.index('async function openElsewhere'):PREVIEW.index('function renderPdf')]
    assert "capPlugin('OpenFile','open')" in block
    assert "if(opened&&opened.ok)return 'opened'" in block
    assert 'PC().saveBlobAs' in block
    assert block.index("capPlugin('OpenFile','open')") < block.index('PC().saveBlobAs')
