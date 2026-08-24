from pathlib import Path

CSS=(Path(__file__).parents[1]/'static/css/client.css').read_text()


def test_mobile_reader_uses_readable_type_and_measure():
    assert '.mail-body{padding:18px 16px 30px;font-size:16px;line-height:1.68' in CSS
    assert '.mail-body .mail-text{max-width:72ch' in CSS
    assert '.mr-subj{font-size:20px;line-height:1.25' in CSS


def test_mobile_actions_are_thumb_sized_and_fit_without_hidden_scrolling():
    assert 'grid-template-columns:repeat(4,minmax(0,1fr))' in CSS
    assert '.mail-actions .btn{width:auto;min-width:0;min-height:44px' in CSS
    assert '.mail-att{min-height:44px' in CSS
