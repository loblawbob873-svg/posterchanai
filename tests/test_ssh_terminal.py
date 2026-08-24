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
import re

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


class _WS:
    def __init__(self, origin, host="poster.place"):
        self.headers = {"origin": origin, "host": host}


def test_the_native_apps_may_open_a_terminal():
    """The APK is Capacitor with androidScheme=https, so its pages are `https://localhost`. The first
    version of this check allowed `http://localhost` and not `https://localhost` — backwards on both
    counts — and the APK over Orbot was refused with "that origin may not open a terminal"."""
    from app.routers.ssh_term import _origin_ok

    for o in ("https://localhost", "capacitor://localhost", "app://posterchan"):
        assert _origin_ok(_WS(o)) is True, o


def test_plaintext_localhost_is_still_refused():
    """The CORS middleware excludes it deliberately: with credentials, any http page on localhost
    could read the victim's authed responses. The socket must not be the softer door."""
    from app.routers.ssh_term import _origin_ok

    assert _origin_ok(_WS("http://localhost")) is False
    assert _origin_ok(_WS("http://localhost:8080")) is False


def test_a_foreign_site_cannot_open_a_shell():
    """A WebSocket upgrade is not covered by the same-origin policy, and the session cookie is
    SameSite=none for the native apps — so without this any page could be handed a shell."""
    from app.routers.ssh_term import _origin_ok

    assert _origin_ok(_WS("https://evil.example")) is False
    assert _origin_ok(_WS("https://poster.place.evil.example")) is False


def test_same_origin_and_no_origin_are_allowed():
    """Same-origin is the web client. No Origin at all is a non-browser client, which cannot be a
    CSRF victim — refusing it would only break scripted use while protecting nobody."""
    from app.routers.ssh_term import _origin_ok

    assert _origin_ok(_WS("https://poster.place")) is True
    assert _origin_ok(_WS("")) is True


def test_the_trust_list_is_shared_with_the_api():
    """Two hand-maintained copies is how one ends up wrong — which is exactly what happened here."""
    import inspect
    from app.routers import ssh_term

    assert "NATIVE_APP_ORIGINS" in inspect.getsource(ssh_term._origin_ok)


# ── A TAB IS A LABEL ──────────────────────────────────────────────────────────────────────────
#
# A remote shell runs inside `tmux new-session -A -s pcai-<uid>-<label>`, which is attach-or-CREATE.
# So the label is not a name for a session — it is the CHOICE of session, and two opens sharing one
# are two SSH connections onto a single shell: same screen, same keystrokes, same scrollback.
#
# That is what "I can never start a new tab on a remote connection. I have 3 server1 connections
# now" was. Nothing failed and nothing logged: the button worked, the session was created, the
# router logged `opened a terminal on server1`, and the prompt that came back was the one already on
# screen. Measured on the live node: three sessions, two of them holding 567,559 and 567,518 bytes.
#
# The client had no `label` anywhere; the keeper path forwarded one that was never sent, and the
# in-process path did not forward it at all.


def test_two_labels_are_two_shells_and_one_label_is_one_shell():
    """The whole mechanism in one assertion: the tmux name is what makes tabs distinct."""
    a = ssh_service._mux_name(7, "main")
    b = ssh_service._mux_name(7, "2")
    assert a != b, "two tabs would share a tmux session — the same shell twice"
    assert ssh_service._mux_name(7, "2") == b, "a resume must be able to name its own shell again"
    # And it is per ACCOUNT: one person's `main` is never another's.
    assert ssh_service._mux_name(8, "main") != a


def test_a_label_is_sanitised_and_never_empty():
    """It lands in a shell command (quoted, but still), and an empty one must not collapse every
    tab into the same unnamed session."""
    assert ssh_service.mux_label("") == "main"
    assert ssh_service.mux_label(None) == "main"
    assert ssh_service.mux_label("2") == "2"
    assert ssh_service.mux_label("a b; rm -rf /") == "abrm-rf"
    assert len(ssh_service.mux_label("x" * 90)) <= 24


def test_a_session_reports_which_tab_it_is():
    """`/api/ssh/sessions` is where the client learns which labels are taken, and it cannot pick a
    free one from a list that does not say. Without this every new tab picks `main`."""
    s = ssh_service.SshSession(user_id=3, host_name="server1")
    assert s.label == "main"                     # before connect, and it is never None
    s.label = "2"
    ssh_service._sessions[s.sid] = s
    try:
        row = [r for r in ssh_service.sessions_for(3) if r["sid"] == s.sid]
        assert row and row[0]["label"] == "2"
    finally:
        ssh_service._sessions.pop(s.sid, None)


def test_connect_records_the_label_it_was_given():
    """`self.label` is what the session list reports, so it must be set from the ARGUMENT rather
    than left at the default — the mux name and the reported name cannot be allowed to disagree."""
    import inspect

    src = inspect.getsource(ssh_service.SshSession.connect)
    assert "self.label = mux_label(label)" in src
    assert "self.mux_name = _mux_name(self.user_id, label)" in src


def test_both_open_paths_forward_the_client_s_label():
    """The keeper path already did; the in-process fallback did not, so a node without the keeper
    had this bug on its own — and that path is the one a fresh install runs."""
    import inspect

    from app.routers import ssh_term

    keeper = inspect.getsource(ssh_term._via_keeper)
    assert 'first.get("label")' in keeper
    assert '"label": label' in keeper

    ws = inspect.getsource(ssh_term.websocket_ssh)
    assert 'label=str(first.get("label") or "main")' in ws, (
        "the in-process open path drops the label, so every new tab takes the server's `main` "
        "default and attaches to the shell already on screen"
    )


def test_every_ready_frame_says_which_tab_it_is():
    """A reconnect has to name the label it was opened under — a resume whose session is gone falls
    through to OPENING one, and an unnamed fallback lands in whichever tab holds `main`."""
    import inspect

    from app.routers import ssh_term
    from app.services import ssh_keeper

    seen = 0
    for src in (inspect.getsource(ssh_term.websocket_ssh),
                inspect.getsource(ssh_term._via_keeper),
                inspect.getsource(ssh_keeper._client)):
        for frame in re.findall(r'\{"t": "ready".*?\}', src, re.S):
            seen += 1
            assert '"label"' in frame, f"a ready frame that does not name its tab: {frame}"
    # A regex that matches nothing passes every assertion it never runs.
    assert seen >= 3, f"only {seen} ready frames found — re-point this test"
