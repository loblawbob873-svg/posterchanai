from pathlib import Path


SRC = (Path(__file__).parents[1] / "scripts/check_qr_device_login.py").read_text()


def test_a_loaded_client_that_rejects_nsec_is_a_failure_not_a_skip():
    """Infrastructure skips are for Chrome/page/relay unavailability, not a dead login button."""
    assert 'return "SKIP " + str(r.get("err") or "the phone would not sign in")' not in SRC
    assert SRC.count("the client loaded, so this is an app failure") == 3
