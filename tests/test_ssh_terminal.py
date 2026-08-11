"""The SSH terminal's GATES — the parts that are dangerous when they are wrong.

Run: venv-unified/bin/python -m pytest tests/test_ssh_terminal.py

This feature is deliberate remote code execution on arbitrary hosts, and the second such path in the
codebase (node_service is the first, and its transport is Nostr-only — SSH was removed from it on
purpose). So what is tested here is not that a shell works; it is that a shell is refused:

  * OFF by default. A setting nobody has touched must not open a terminal.
  * The host list is an ALLOWLIST. The client sends a NAME, and a name that is not configured
    resolves to nothing — a client cannot name an address, or "a terminal that can ssh" is a proxy
    into every machine this server can route to, including the ones behind it.
  * Only admins and named users, mirroring node_service's rule rather than inventing a second one.
  * A malformed host line is SKIPPED, never half-parsed into a destination nobody meant.

The PTY itself is not exercised — that needs a real sshd — but nothing above depends on it.
"""
import pytest

from app.services import ssh_service


from app.services.nostr import nostr_service

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64
NPUB_A = nostr_service.npub_of(HEX_A)
NPUB_B = nostr_service.npub_of(HEX_B)


class _User:
    """Shaped like the real `User` row, and that is load-bearing.

    The first version of this carried `nostr_pubkey`, an attribute the model does not have — so the
    test passed against a rule that could never match a real user, and the allowlist shipped dead.
    A stub is for isolating a dependency, not for inventing one: the column is `nostr_npub` and it
    stores HEX, while the SETTING is written as an `npub1…`, which is why both sides have to go
    through the same canonicaliser."""

    def __init__(self, is_admin=False, nostr_npub=None, username=None, id=2):
        self.is_admin = is_admin
        self.nostr_npub = nostr_npub
        self.username = username
        self.id = id


@pytest.fixture
def settings(monkeypatch):
    store = {}
    monkeypatch.setattr(ssh_service.settings_store, "get", lambda k: store.get(k))
    return store


def test_it_is_off_until_switched_on(settings):
    assert ssh_service.is_enabled() is False
    settings["ssh_terminal_enabled"] = "true"
    assert ssh_service.is_enabled() is True


def test_only_the_exact_word_true_enables_it(settings):
    """A half-written value must not open a shell. Anything that is not "true" is off."""
    for v in ("yes", "1", "TRUE ", "", "on", "false"):
        settings["ssh_terminal_enabled"] = v
        assert ssh_service.is_enabled() is (v.strip().lower() == "true"), v


def test_a_host_line_is_parsed(settings):
    settings["ssh_hosts"] = "build  deploy@10.0.0.9:2222  key=/home/pc/.ssh/id_ed25519"
    h = ssh_service.hosts()["build"]
    assert (h.user, h.host, h.port, h.key) == ("deploy", "10.0.0.9", 2222, "/home/pc/.ssh/id_ed25519")


def test_the_port_defaults_and_the_key_is_optional(settings):
    settings["ssh_hosts"] = "nas admin@nas.lan"
    h = ssh_service.hosts()["nas"]
    assert (h.port, h.key) == (22, "")


def test_a_malformed_line_is_skipped_not_guessed_at(settings):
    """Half-parsing a destination is how a terminal ends up pointed somewhere nobody chose."""
    settings["ssh_hosts"] = "\n".join([
        "good  me@host.example",
        "nouser  host.example",          # no user@
        "  ",                             # blank
        "# a comment",
        "bad host with spaces@@",
        "alsogood  root@10.1.1.1:22",
    ])
    assert sorted(ssh_service.hosts()) == ["alsogood", "good"]


def test_an_out_of_range_port_is_dropped(settings):
    settings["ssh_hosts"] = "weird me@host.example:99999"
    assert ssh_service.hosts() == {}


def test_the_first_definition_of_a_name_wins(settings):
    """A duplicate name would otherwise make which host you reach depend on list order."""
    settings["ssh_hosts"] = "dup a@first.example\ndup b@second.example"
    assert ssh_service.hosts()["dup"].host == "first.example"


def test_an_unlisted_name_resolves_to_nothing(settings):
    """THE allowlist property: the socket looks a name up in this dict, so anything not here cannot
    be reached — the client has no way to pass an address at all."""
    settings["ssh_hosts"] = "build me@10.0.0.9"
    assert ssh_service.hosts().get("evil.example") is None
    assert ssh_service.hosts().get("10.0.0.9") is None


def test_an_admin_is_always_allowed(settings):
    assert ssh_service.user_allowed(None, _User(is_admin=True)) is True


def test_nobody_else_is_allowed_by_default(settings):
    """An empty allowlist means admin-only, NOT everyone."""
    assert ssh_service.user_allowed(None, _User(nostr_npub=HEX_A)) is False


def test_a_named_user_is_allowed(settings):
    """The user's key is stored as HEX and the setting is typed as an npub — the whole reason both
    sides go through nostr_service.to_pubkey_hex. Comparing the raw strings never matches."""
    settings["ssh_terminal_users"] = f"{NPUB_A}, {NPUB_B}"
    assert ssh_service.user_allowed(None, _User(nostr_npub=HEX_B)) is True
    assert ssh_service.user_allowed(None, _User(nostr_npub=HEX_C)) is False


def test_a_hex_key_in_the_setting_also_matches(settings):
    """An operator may paste either form; both canonicalise to the same key."""
    settings["ssh_terminal_users"] = HEX_A
    assert ssh_service.user_allowed(None, _User(nostr_npub=HEX_A)) is True


def test_the_first_signup_is_always_allowed(settings):
    """id == 1 is the admin account, the same exemption node_exec makes."""
    assert ssh_service.user_allowed(None, _User(id=1)) is True


def test_a_user_with_no_key_is_refused(settings):
    settings["ssh_terminal_users"] = NPUB_A
    assert ssh_service.user_allowed(None, _User(username="someone")) is False


def test_no_user_is_not_a_user(settings):
    settings["ssh_terminal_users"] = "npub1abc"
    assert ssh_service.user_allowed(None, None) is False


def test_the_settings_are_declared_so_they_hydrate():
    """A key missing from SettingsResponse is dropped from the GET, so the admin checkbox loads blank
    and posts `false` over the stored value on the next Save — silently switching the feature off.
    That has happened to four settings in this codebase already."""
    from app.schemas import SettingsResponse
    fields = SettingsResponse.model_fields
    for k in ("ssh_terminal_enabled", "ssh_terminal_users", "ssh_hosts"):
        assert k in fields, f"{k} is not declared in SettingsResponse"
    assert SettingsResponse().ssh_terminal_enabled == "false", "the terminal must default to OFF"


def test_a_quoted_comment_does_not_take_down_the_whole_list(settings):
    """`shlex.split` raises on an unbalanced quote, and an apostrophe in a trailing comment is one.
    That exception escaped the per-line guard, so ONE line like this 500'd /api/ssh/hosts and killed
    the socket — for every host, not just the bad one."""
    settings["ssh_hosts"] = "\n".join([
        "nas  admin@nas.lan  # don't touch this one",
        "build  deploy@10.0.0.9",
    ])
    got = ssh_service.hosts()
    assert sorted(got) == ["build", "nas"]
    assert got["nas"].host == "nas.lan"


def test_a_trailing_comment_is_not_read_as_an_option(settings):
    settings["ssh_hosts"] = "nas admin@nas.lan  # key=/not/a/real/key"
    assert ssh_service.hosts()["nas"].key == ""


def test_the_failure_reason_never_carries_the_exception_text():
    """paramiko names the server-side private-key path in its messages, which /api/ssh/hosts
    deliberately withholds. The KIND of failure is what a person can act on; the text is not."""
    from app.routers.ssh_term import _why

    class AuthenticationException(Exception):
        pass

    msg = _why(AuthenticationException("/home/pc/.ssh/id_ed25519 rejected"))
    assert "id_ed25519" not in msg and "/home" not in msg
    assert "credentials" in msg


def test_every_failure_still_says_something_useful():
    from app.routers.ssh_term import _why
    assert _why(TimeoutError("x")) != ""
    assert _why(ConnectionRefusedError("x")) != ""
    assert _why(RuntimeError("x")) != ""


def test_a_public_key_says_so_instead_of_blaming_the_handshake():
    """The single most likely misconfiguration: `key=` pointed at `id_rsa.pub`. paramiko raises a
    plain SSHException whose message is "not a valid OPENSSH private key file", which the first
    classifier flattened to "the SSH handshake failed" — sending you to look at the network for a
    filename problem. Verified against the real exception paramiko raises."""
    from app.routers.ssh_term import _why
    import paramiko

    m = _why(paramiko.SSHException("not a valid OPENSSH private key file"))
    assert ".pub" in m and "private" in m
    assert "handshake" not in m


def test_an_encrypted_key_is_not_reported_as_a_public_one():
    """paramiko says "Private key file is encrypted", which matches the .pub test too — order matters,
    and the passphrase answer is the useful one."""
    from app.routers.ssh_term import _why
    import paramiko

    assert "passphrase" in _why(paramiko.SSHException("Private key file is encrypted"))


def test_a_missing_key_file_is_named_as_such():
    from app.routers.ssh_term import _why
    import paramiko

    assert "not on the server" in _why(paramiko.SSHException("No such file or directory"))
