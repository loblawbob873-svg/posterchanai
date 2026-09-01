import re
from pathlib import Path

CSS=(Path(__file__).parents[1]/'static/css/client.css').read_text()
ROOT=Path(__file__).parents[1]


def test_mobile_reader_uses_readable_type_and_measure():
    assert '.mail-body{padding:18px 16px 30px;font-size:16px;line-height:1.68' in CSS
    assert '.mail-body .mail-text{max-width:72ch' in CSS
    assert '.mr-subj{font-size:20px;line-height:1.25' in CSS


def test_single_html_message_fills_reader_without_a_decorative_document_inset():
    # THE ONE-MESSAGE THREAD, however it is spelled. This named `:only-child`, which stopped being
    # true the moment the thread grew a Reply/Forward row beneath the message: the message is then
    # the first of two children, not the only one. The PROPERTY is "a lone HTML message fills the
    # pane"; the selector expressing it is an implementation detail, and changing it is not a
    # regression.
    lone = ('.mail-thread>.mail-msg:only-child',
            '.mail-thread>.mail-msg:first-child:nth-last-child(2)')
    assert any(sel in CSS for sel in lone), (
        "nothing makes a single message fill the reader pane any more: " + repr(lone))
    for sel in lone:
        if sel in CSS:
            assert sel + '>.mail-msg-body{flex:1' in CSS, (
                "the rule exists but no longer stretches the message body, so a lone HTML mail "
                "would sit in a short box with dead space under it")
    assert '.mail-thread:only-child .mail-msg:only-child' not in CSS
    assert '.mail-body:has(>.mail-html){padding:0}' in CSS
    assert '.mail-html{width:100%;border:none;background:#fff;border-radius:0;' in CSS


def test_mobile_actions_are_thumb_sized_and_fit_without_hidden_scrolling():
    assert 'grid-template-columns:repeat(7,minmax(0,1fr))' in CSS
    assert '.mail-actions .btn{width:auto;min-width:0;min-height:40px;height:40px' in CSS
    assert '.mail-att{min-height:44px' in CSS


def test_mobile_reader_actions_are_compact_accessible_icons():
    app = (ROOT / 'static/js/client/app.js').read_text()
    thread = app[app.index('_renderThread(pane, thread, folder, acct, seedUid)'):
                 app.index('_msgText(msg)', app.index('_renderThread(pane, thread, folder, acct, seedUid)'))]
    # SCOPED TO THE ACTIONS ROW, not to the whole function. This counted every button in
    # `_renderThread`, so adding a labelled Reply/Forward row elsewhere in the thread failed a test
    # about the compact icon row — which was still exactly as it should be. The row is the subject;
    # the count of buttons anywhere in the render is not.
    row = re.search(r'<div class="(?:mail-msg-actions|mail-actions)[^"]*"(.*?)</div>', thread, re.S)
    assert row, "the reader's actions row has moved — re-read this test"
    assert row.group(1).count('class="btn') == 7
    # Compactness and labelling are asserted over the whole thread on purpose: an icon-only button
    # without an aria-label is unreachable by a screen reader wherever it is put.
    assert thread.count('icon-only') == 7
    assert thread.count('aria-label=') == thread.count('icon-only')


def test_packaged_mail_attachments_use_the_configured_instance():
    app = (ROOT / 'static/js/client/app.js').read_text()
    assert 'function _mailAttachmentUrl(' in app
    assert "if(!/^https?:\\/\\//i.test(base)) return '';" in app
    assert 'data-mail-url="${enc(url)}"' in app


def test_viewable_mail_attachments_open_in_the_fitted_preview_app():
    app = (ROOT / 'static/js/client/app.js').read_text()
    render = app[app.index('_msgBlock(m, folder, acct, expanded)'):
                 app.index('_nmailHtml(nm, m)', app.index('_msgBlock(m, folder, acct, expanded)'))]
    thread = app[app.index('_renderThread(pane, thread, folder, acct, seedUid)'):
                 app.index('_msgText(msg)', app.index('_renderThread(pane, thread, folder, acct, seedUid)'))]
    assert '_previewable(name,type)' in render
    assert 'data-mail-preview="1"' in render
    assert 'await _openMailAttachment(a)' in thread
    opener = app[app.index('async function _openMailAttachment'):app.index('const Mail =', app.index('async function _openMailAttachment'))]
    assert "fetch(url,{credentials:'include'" in opener
    assert "_withModule('preview.js','PCPreview')" in opener
    assert "P.open({name:a.dataset.name||'attachment'" in opener


def test_every_mail_attachment_uses_authenticated_fetch_then_preview_or_save():
    app = (ROOT / 'static/js/client/app.js').read_text()
    render = app[app.index('_msgBlock(m, folder, acct, expanded)'):
                 app.index('_nmailHtml(nm, m)', app.index('_msgBlock(m, folder, acct, expanded)'))]
    thread = app[app.index('_renderThread(pane, thread, folder, acct, seedUid)'):
                 app.index('_msgText(msg)', app.index('_renderThread(pane, thread, folder, acct, seedUid)'))]
    assert 'data-mail-attachment="1"' in render
    assert "$$('[data-mail-attachment]',pane)" in thread
    opener = app[app.index('async function _openMailAttachment'):app.index('const Mail =', app.index('async function _openMailAttachment'))]
    assert "'Authorization':'Bearer '+_aiToken" in opener
    assert "if(a.dataset.mailPreview==='1')" in opener
    assert "else await saveBlobAs(blob,a.dataset.name||'attachment')" in opener
