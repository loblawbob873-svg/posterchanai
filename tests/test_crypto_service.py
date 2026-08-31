"""THE AT-REST KEY THAT, IF LOST, TAKES EVERY STORED MAIL PASSWORD WITH IT.

`crypto_service.py` had ZERO test references. It is the Fernet layer protecting sensitive values at
rest — `mail_service` runs users' IMAP/SMTP passwords through it — and the module's own code says
what happens when its key file cannot be written:

    Key will be regenerated on restart - encrypted data will be lost!

Three behaviours here are decisions, not implementation details, and all three are the kind that
keep working locally while being wrong:

  * **`decrypt_string` returns `""` on failure, never raises.** Deliberate ("Return empty to prevent
    using corrupted data") and load-bearing in both directions: it means a wrong key does not crash
    the app, and it means a wrong key is INDISTINGUISHABLE from an empty password unless something
    pins it. If it ever returned the raw ciphertext instead, that string would be handed to an SMTP
    server as a password.
  * **A value with no `enc:` prefix is returned verbatim.** That is the migration path for rows
    written before encryption existed. Remove it and every legacy row silently becomes `""` — a
    mail account that stops authenticating with no error anywhere.
  * **The key file is chmod 0600.** It is the whole security boundary; a mode regression is
    invisible until somebody else is on the box.

EVERY TEST HERE IS ISOLATED FROM THE REAL KEY. `KEY_FILE` points at the project root and the live
file exists (44 bytes, 0600, gitignored). A test that let `_get_or_create_key` run unpatched would
either read the operator's real key or, worse, write over it — the rule this repo already broke once
by touching `streamserver/mediamtx.pid`. Every test below redirects `KEY_FILE` into `tmp_path` and
resets the cached `_fernet`.
"""
import os
import stat

import pytest
from cryptography.fernet import Fernet

from app.services import crypto_service as cs


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A throwaway key, in a throwaway directory. Never the node's real one."""
    monkeypatch.setattr(cs, "KEY_FILE", tmp_path / ".encryption_key")
    monkeypatch.setattr(cs, "_fernet", None)
    monkeypatch.delenv("POSTERCHANAI_ENCRYPTION_KEY", raising=False)
    return tmp_path


def test_the_real_key_file_is_never_touched_by_this_module_under_test(isolated):
    """Guards the guard: if the fixture ever stops redirecting KEY_FILE, these tests would start
    operating on the live key and this is the test that says so."""
    assert cs.KEY_FILE.parent == isolated
    assert "posterchanai/.encryption_key" not in str(cs.KEY_FILE)


# --------------------------------------------------------------------------- round trip


def test_a_string_round_trips(isolated):
    assert cs.decrypt_string(cs.encrypt_string("hunter2")) == "hunter2"


@pytest.mark.parametrize("value", ["a", "p@ss word!", "üñïçødé ✓", "x" * 10_000,
                                   "line\nbreak\ttab", "enc-but-not-a-prefix"])
def test_awkward_values_round_trip(isolated, value):
    assert cs.decrypt_string(cs.encrypt_string(value)) == value


def test_the_ciphertext_does_not_contain_the_plaintext(isolated):
    assert "hunter2" not in cs.encrypt_string("hunter2")


def test_encryption_is_not_deterministic(isolated):
    """Fernet embeds a random IV. Identical ciphertexts would tell anyone with database access
    which users share a password."""
    assert cs.encrypt_string("same") != cs.encrypt_string("same")


def test_the_encrypted_form_is_marked_with_the_prefix(isolated):
    out = cs.encrypt_string("hunter2")
    assert out.startswith("enc:")
    assert cs.is_encrypted(out)


# --------------------------------------------------------------------------- the legacy path


def test_a_plaintext_value_is_returned_unchanged(isolated):
    """The migration path. Without it every row written before encryption existed decrypts to ""
    — a stored mail password that silently becomes empty, with nothing in any log."""
    assert cs.decrypt_string("legacy-plaintext-password") == "legacy-plaintext-password"
    assert cs.is_encrypted("legacy-plaintext-password") is False


def test_empty_values_pass_straight_through(isolated):
    for empty in ("", None):
        assert cs.encrypt_string(empty) == empty
        assert cs.decrypt_string(empty) == empty
    assert cs.is_encrypted("") is False
    assert cs.is_encrypted(None) is False


# --------------------------------------------------------------------------- failure is empty


def test_the_wrong_key_yields_empty_and_never_the_ciphertext(isolated, monkeypatch):
    """The documented decision. The dangerous alternative is not a crash — it is returning the
    `enc:...` blob, which would then be sent to an SMTP server as the password."""
    token = cs.encrypt_string("hunter2")

    monkeypatch.setattr(cs, "_fernet", Fernet(Fernet.generate_key()))
    out = cs.decrypt_string(token)
    assert out == ""
    assert "enc:" not in out and "hunter2" not in out


def test_corrupt_ciphertext_yields_empty_rather_than_raising(isolated):
    """A truncated or edited column must not take down whatever was reading it."""
    token = cs.encrypt_string("hunter2")
    for bad in (token[:-4], "enc:not-base64-at-all", "enc:", "enc:AAAA"):
        assert cs.decrypt_string(bad) == ""


def test_a_value_that_merely_starts_with_enc_is_not_mistaken_for_a_token(isolated):
    """`is_encrypted` is a prefix check, so a legitimate secret beginning with `enc:` decrypts to
    "" — worth pinning as known behaviour rather than discovering it as a lost password."""
    assert cs.is_encrypted("enc:this is actually someone's password") is True
    assert cs.decrypt_string("enc:this is actually someone's password") == ""


# --------------------------------------------------------------------------- the key file


def test_a_generated_key_file_is_owner_only(isolated):
    """0600 is the entire boundary protecting every stored password on the node."""
    cs.get_fernet()
    assert cs.KEY_FILE.exists(), "no key file was written"
    mode = stat.S_IMODE(os.stat(cs.KEY_FILE).st_mode)
    assert mode == 0o600, f"key file is mode {oct(mode)}, not 0600"


def test_an_existing_key_file_is_reused_not_regenerated(isolated):
    """"Key will be regenerated on restart - encrypted data will be lost!" — this is that failure.
    A restart that mints a fresh key makes every stored value undecryptable."""
    token = cs.encrypt_string("hunter2")
    first = cs.KEY_FILE.read_bytes()

    cs._fernet = None                                   # simulate a process restart
    assert cs.decrypt_string(token) == "hunter2"
    assert cs.KEY_FILE.read_bytes() == first


def test_the_environment_key_wins_over_the_file(isolated, monkeypatch):
    """The documented precedence, and the one an operator relies on to hold the key outside the
    working tree (a container secret, a systemd credential)."""
    env_key = Fernet.generate_key()
    cs.KEY_FILE.write_bytes(Fernet.generate_key())      # a DIFFERENT key on disk
    monkeypatch.setenv("POSTERCHANAI_ENCRYPTION_KEY", env_key.decode())
    monkeypatch.setattr(cs, "_fernet", None)

    assert cs._get_or_create_key() == env_key


def test_a_junk_environment_key_falls_back_instead_of_crashing(isolated, monkeypatch):
    """A mistyped secret must not take the whole app down at import time."""
    monkeypatch.setenv("POSTERCHANAI_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    monkeypatch.setattr(cs, "_fernet", None)
    assert cs.decrypt_string(cs.encrypt_string("hunter2")) == "hunter2"


def test_a_corrupt_key_file_is_replaced_rather_than_crashing(isolated):
    """Same reasoning from the other side: a truncated key file must not make the node unbootable.
    Old data is unreadable either way — this only decides whether anything still starts."""
    cs.KEY_FILE.write_bytes(b"garbage-not-a-key")
    assert cs.decrypt_string(cs.encrypt_string("hunter2")) == "hunter2"


def test_a_whitespace_padded_key_file_still_loads(isolated):
    """`.strip()` is in the read path, so a key file with a trailing newline — what any editor or
    `echo` produces — must work. Without it the key is 'invalid' and gets regenerated, which is the
    total-loss path dressed up as a fresh install."""
    key = Fernet.generate_key()
    cs.KEY_FILE.write_bytes(key + b"\n")
    assert cs._get_or_create_key() == key


def test_the_fernet_instance_is_cached(isolated):
    """`get_fernet` memoises; without it every encrypt re-reads the file from disk."""
    assert cs.get_fernet() is cs.get_fernet()
