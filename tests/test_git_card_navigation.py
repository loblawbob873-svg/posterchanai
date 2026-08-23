from pathlib import Path

SRC=(Path(__file__).parents[1]/'static/js/client/git.js').read_text()


def test_repo_cards_keep_the_event_they_rendered():
    assert 'let _repoEvents = new Map()' in SRC
    assert '_repoEvents = new Map(repos.map(e=>[e.id,e]))' in SRC
    assert '_repoEvents.get(c.dataset.id)||Store.get(c.dataset.id)' in SRC


def test_card_click_cannot_fall_through_to_second_global_handler():
    assert "c.onclick=ev=>{ ev.stopPropagation();" in SRC
    assert 'this repository is no longer available — refresh Git' in SRC

