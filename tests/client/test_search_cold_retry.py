from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_cold_search_has_two_bounded_retries_and_rechecks_the_socket():
    src = (ROOT / "static/js/client/app.js").read_text()
    start = src.index("for(let retry=0; postEvs && postEvs.complete === false")
    block = src[start:start + 1100]
    assert "retry<2" in block
    assert "900 * (retry + 1)" in block
    assert "await Relay.ready(4000)" in block
    assert block.count("Relay.query([{ kinds:[1], search:q, limit:40 }])") == 1
    assert "VIEW!=='search'" in block
