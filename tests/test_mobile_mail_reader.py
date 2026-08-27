from pathlib import Path

CSS=(Path(__file__).parents[1]/'static/css/client.css').read_text()
ROOT=Path(__file__).parents[1]


def test_mobile_reader_uses_readable_type_and_measure():
    assert '.mail-body{padding:18px 16px 30px;font-size:16px;line-height:1.68' in CSS
    assert '.mail-body .mail-text{max-width:72ch' in CSS
    assert '.mr-subj{font-size:20px;line-height:1.25' in CSS


def test_mobile_actions_are_thumb_sized_and_fit_without_hidden_scrolling():
    assert 'grid-template-columns:repeat(7,minmax(0,1fr))' in CSS
    assert '.mail-actions .btn{width:auto;min-width:0;min-height:40px;height:40px' in CSS
    assert '.mail-att{min-height:44px' in CSS


def test_mobile_reader_actions_are_compact_accessible_icons():
    app = (ROOT / 'static/js/client/app.js').read_text()
    thread = app[app.index('_renderThread(pane, thread, folder, acct, seedUid)'):
                 app.index('_msgText(msg)', app.index('_renderThread(pane, thread, folder, acct, seedUid)'))]
    assert thread.count('class="btn') == 7
    assert thread.count('icon-only') == 7
    assert thread.count('aria-label=') == 7


def test_packaged_mail_attachments_use_the_configured_instance():
    app = (ROOT / 'static/js/client/app.js').read_text()
    assert 'const dlBase=_instanceBase();' in app
    assert 'href="${enc(dlBase)}/api/mail/dl/' in app


def test_viewable_mail_attachments_open_in_the_fitted_preview_app():
    app = (ROOT / 'static/js/client/app.js').read_text()
    render = app[app.index('_msgBlock(m, folder, acct, expanded)'):
                 app.index('_nmailHtml(nm, m)', app.index('_msgBlock(m, folder, acct, expanded)'))]
    thread = app[app.index('_renderThread(pane, thread, folder, acct, seedUid)'):
                 app.index('_msgText(msg)', app.index('_renderThread(pane, thread, folder, acct, seedUid)'))]
    assert '_previewable(name,type)' in render
    assert 'data-mail-preview="1"' in render
    assert "fetch(a.href,{credentials:'include'" in thread
    assert "_withModule('preview.js','PCPreview')" in thread
    assert "P.open({name:a.dataset.name||'attachment'" in thread


def test_every_mail_attachment_uses_authenticated_fetch_then_preview_or_save():
    app = (ROOT / 'static/js/client/app.js').read_text()
    render = app[app.index('_msgBlock(m, folder, acct, expanded)'):
                 app.index('_nmailHtml(nm, m)', app.index('_msgBlock(m, folder, acct, expanded)'))]
    thread = app[app.index('_renderThread(pane, thread, folder, acct, seedUid)'):
                 app.index('_msgText(msg)', app.index('_renderThread(pane, thread, folder, acct, seedUid)'))]
    assert 'data-mail-attachment="1"' in render
    assert "$$('[data-mail-attachment]',pane)" in thread
    assert "'Authorization':'Bearer '+_aiToken" in thread
    assert "if(a.dataset.mailPreview==='1')" in thread
    assert "else await saveBlobAs(blob,a.dataset.name||'attachment')" in thread
