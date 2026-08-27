from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PLUGIN=(ROOT/'mobile/android/app/src/main/java/place/poster/app/preview/OpenFilePlugin.java').read_text()
MAIN=(ROOT/'mobile/android/app/src/main/java/place/poster/app/MainActivity.java').read_text()
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


def test_native_open_is_private_bounded_and_cleans_previous_preview():
    assert 'getCacheDir(),"preview-open"' in PLUGIN
    assert 'MAX = 32 * 1024 * 1024' in PLUGIN
    assert 'for(File f:old)if(f!=null)f.delete()' in PLUGIN
    assert 'getExternal' not in PLUGIN


def test_pdf_viewer_failure_retains_save_or_share_fallback():
    block=PREVIEW[PREVIEW.index('async function openElsewhere'):PREVIEW.index('async function renderPdf')]
    assert "capPlugin('OpenFile','open')" in block
    assert "if(opened&&opened.ok)return 'opened'" in block
    assert 'PC().saveBlobAs' in block
    assert block.index("capPlugin('OpenFile','open')") < block.index('PC().saveBlobAs')
