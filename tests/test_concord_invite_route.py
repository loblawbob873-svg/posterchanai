"""A CORD invite is a client route; its fragment is decrypted only in the browser."""

from fastapi.testclient import TestClient

import app.main as main


client = TestClient(main.app)


def test_concord_invite_path_serves_the_client_instead_of_a_detail_404():
    # Syntactically valid bech32 alphabet is sufficient here. The browser/protocol library performs
    # the full naddr and encrypted-fragment validation without ever sending the fragment to FastAPI.
    response = client.get("/invite/naddr1" + "q" * 24)
    assert response.status_code == 200
    assert 'data-view="messages"' in response.text
    assert 'static/js/client/concord.js' in response.text


def test_concord_invite_route_does_not_accept_arbitrary_paths():
    assert client.get("/invite/not-an-naddr").status_code == 404
    assert client.get("/invite/npub1" + "q" * 24).status_code == 404
