from pathlib import Path

CSS=(Path(__file__).parents[1]/'static/css/client.css').read_text()


def test_mobile_reader_uses_readable_type_and_measure():
    assert '.mail-body{padding:18px 16px 30px;font-size:16px;line-height:1.68' in CSS
    assert '.mail-body .mail-text{max-width:72ch' in CSS
    assert '.mr-subj{font-size:20px;line-height:1.25' in CSS


def test_mobile_actions_are_thumb_sized_and_scroll_instead_of_crushing():
    assert '.mail-actions .btn{flex:0 0 44px;width:44px;min-height:44px' in CSS
    assert 'overflow-x:auto' in CSS
    assert '.mail-att{min-height:44px' in CSS

